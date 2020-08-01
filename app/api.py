# -*- coding: utf-8 -*-
""" app.api
~~~~~~~~~~~~

Live Site:

Endpoints:
"""
import random, re

from json import dumps, load, loads
from json.decoder import JSONDecodeError
from functools import partial
from urllib.parse import parse_qsl, urlparse
from io import BytesIO
from shutil import copyfileobj
from datetime import datetime as dt, date, timedelta
from collections import OrderedDict

import requests
import inflect
import boto3
import pygogo as gogo

from botocore.exceptions import ClientError, WaiterError, ProfileNotFound

from faker import Faker
from flask import (
    Blueprint,
    after_this_request,
    current_app as app,
    request,
    url_for,
    redirect,
    session,
)

from flask.views import MethodView
from rq import Queue

from meza.convert import records2csv
from config import Config
from app import cache
from app.utils import (
    responsify,
    jsonify,
    parse_kwargs,
    cache_header,
    make_cache_key,
    uncache_header,
    title_case,
    get_common_rel,
    get_request_base,
    get_links,
)
from app.connection import conn

# https://requests-oauthlib.readthedocs.io/en/latest/index.html
# https://oauth-pythonclient.readthedocs.io/en/latest/index.html
q = Queue(connection=conn)
p = inflect.engine()
blueprint = Blueprint("API", __name__)
fake = Faker()

try:
    session = boto3.Session(profile_name="nerevu")
except ProfileNotFound:
    boto_kwargs = {
        "aws_access_key_id": Config.AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": Config.AWS_SECRET_ACCESS_KEY,
        "region_name": Config.AWS_REGION,
    }

    session = boto3.Session(**boto_kwargs)

s3_client = session.client("s3")
s3_resource = session.resource("s3")


logger = gogo.Gogo(__name__, monolog=True).logger

# these don't change based on mode, so no need to do app.config['...']
PREFIX = Config.API_URL_PREFIX
HEADERS = {"Accept": "application/json"}
ROUTE_TIMEOUT = Config.ROUTE_TIMEOUT
SET_TIMEOUT = Config.SET_TIMEOUT
LRU_CACHE_SIZE = Config.LRU_CACHE_SIZE
BASE_URL = Config.BASE_URL
S3_BUCKET = Config.S3_BUCKET
S3_DATE_FORMAT = Config.S3_DATE_FORMAT
REPORT_CONFIGS = Config.REPORT_CONFIGS
COVID_CSV_PATHS = Config.COVID_CSV_PATHS
CKAN_API_KEY = Config.CKAN_API_KEY
CKAN_API_BASE_URL = Config.CKAN_API_BASE_URL

DAYS = Config.DAYS

JOB_STATUSES = {
    "deferred": 202,
    "queued": 202,
    "started": 202,
    "finished": 200,
    "failed": 500,
    "job not found": 404,
}


def _clear_cache():
    cache.delete(f"GET:{PREFIX}/data")


def _get_last_update_date(use_s3=True, bucket_name=S3_BUCKET, **kwargs):
    report_type = kwargs["report_type"]
    config = REPORT_CONFIGS[report_type]
    prefix = config["filename"].split("{}")[0]

    if use_s3:
        bucket = s3_resource.Bucket(bucket_name)
        objs = bucket.objects.filter(Prefix=prefix)
        filenames = sorted(s3_obj.key for s3_obj in objs)
        newest_filename = filenames[-1]
        newest_date = newest_filename.split("_")[-1].split(".")[0]
        last_updated = dt.strptime(newest_date, S3_DATE_FORMAT)
    else:
        last_updated = None

    return last_updated


def get_job_result(job):
    with app.test_request_context():
        if job:
            job_status = job.get_status()
            job_result = job.result
            job_id = job.id
        else:
            job_status = "job not found"
            job_result = {}
            job_id = 0

        result = {
            "status_code": JOB_STATUSES[job_status],
            "job_id": job_id,
            "job_status": job_status,
            "job_result": job_result,
            "url": url_for(".result", job_id=job_id, _external=True),
        }

        return {"ok": job_status != "failed", "result": result}


def get_job_result_by_id(job_id):
    """ Displays a job result.

    Args:
        job_id (str): The job id.
    """
    job = q.fetch_job(job_id)
    return get_job_result(job)


def get_error_resp(e, status_code=500):
    return {
        "ok": False,
        "message": str(e),
        "status_code": status_code,
    }


def read_zipcodes(json, path):
    data = loads(json)
    records = data['zipcodes']
    for zips in records:
        keys_to_remove = []
        for key in zips['demographics'].keys():
            if key != path:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del zips['demographics'][key]
        print()
    yield records


def flatten(d,sep="_"):
    obj = OrderedDict()

    for i in d:
        def recurse(t,parent_key=""):
            if isinstance(t,list):
                for j in range(len(t)):
                    recurse(t[j],parent_key + sep + str(j) if parent_key else str(j))
            elif isinstance(t,dict):
                for k,v in t.items():
                    recurse(v,parent_key + sep + k if parent_key else k)
            else:
                obj[parent_key] = t

        if isinstance(i, dict):
            i = [i]
        recurse(i)

    yield obj


def json2rows(src, path):
    obj = {}
    for i in flatten(src, sep='-'):
        for k,v in i.items():
            index = "-".join(re.findall(r'\d+', k))
            split_key = re.split(r'\d+-', k)
            depth = len(split_key) - 1
            new_key = "".join(split_key)
            if new_key not in path['blacklist']:
                if new_key in path['change']:
                    new_key = path['change'][new_key]
                if depth in obj and index in obj[depth]:
                    obj[depth][index] = {**obj[depth][index], **{new_key: v}}
                elif depth in obj:
                    obj[depth][index] = {new_key: v}
                else:
                    obj[depth] = {}
                    obj[depth][index] = {new_key: v}

    for index in obj[max(obj.keys())].keys():
        accumulator = ''
        row = {}
        for pos, num in enumerate(index.split('-')):
            pos += 1
            if pos == 1:
                accumulator += num
            else:
                accumulator += '-' + num

            row = {**row, **obj[pos][accumulator]}
        yield row


def read_json(src, path):
    data = loads(src)
    # TODO: this could be more resilient
    for key in path.split('.'):
        data = data[key]
    yield data


def json2csvs(src, report_type):
    if isinstance(src, str):
        paths = COVID_CSV_PATHS[report_type]
        for path in paths:
            gen_records = read_zipcodes if report_type == 'zip' else read_json
            records = gen_records(src, path=path['path'])
            rows_gen = json2rows(records, path)
            csv_report = records2csv(rows_gen)
            yield {
                'src': csv_report.read(),
                'filename': f'{report_type}.{path["path"]}'
            }


def prep_for_upload(csvs, base_filename):
    # TODO: perhaps change the REPORT_CONFIGS filename instead
    for src in csvs:
        yield {
            'src': src['src'],
            'filename': f"{base_filename.replace('.json', '')}-{src['filename']}.csv",
            'mimetype': 'text/csv',
        }


def save_ckan_report(src, report_date, **kwargs):
    report_type = kwargs["report_type"]
    config = REPORT_CONFIGS[report_type]
    filename = config["filename"].format(report_date)
    headers = {"X-CKAN-API-Key": CKAN_API_KEY}
    src_csvs = json2csvs(src, report_type)
    csv_items = prep_for_upload(src_csvs, filename)
    json_item = [{
        'src': src,
        'filename': filename,
        'mimetype': 'application/json'
    }]
    responses = {}

    # this only works for the first date if the source isn't s3
    # because the data is less detailed on other days
    for item in json_item + list(csv_items):
        # check for data before uploading it
        response = None
        r = requests.post(
            # resource_search by name matches any files that have
            # the {filename} in the file name (not an exact match)
            f"{CKAN_API_BASE_URL}/resource_search",
            data={"query": f"name:{item['filename']}"},
            headers=headers,
        )
        json = r.json()

        action = 'updated' if json['result']['count'] else 'created'
        overwrite = kwargs.get('overwrite')
        is_update = action == 'updated'

        if overwrite and is_update:
            # setup post_data for updating new resource
            post_data = {
                "url": CKAN_API_BASE_URL + '/resource_update',
                "data": {"id": json['result']['results'][0]['id']}
            }
        elif is_update:
            response = get_error_resp(f"{item['filename']} already exists!")
        else:
            # setup post_data for creating new resource
            # (https://docs.ckan.org/en/2.8/api/index.html#ckan.logic.action.create.resource_create)
            post_data = {
                "url": CKAN_API_BASE_URL + '/resource_create',
                "data": {
                    "package_id": config["package_id"],
                    "mimetype": item['mimetype'],
                    "format": item['mimetype'],
                    "name": item['filename'],
                },
                "files": [('upload', item['src'])]
            }

        if not response:
            # upload data
            post_data['headers'] = headers
            r = requests.post(**post_data)
            json = r.json()
            if r.ok:
                response = {
                    "ok": True,
                    "message": f"Successfully {action} {item['filename']}!",
                    "last_modified": json['result'].get("last_modified"),
                    # TODO: should I use name here?
                    "etag": json['result'].get("name", "").strip('"'),
                    "content_length": json['result'].get("content_length"),
                    "status_code": r.status_code,
                }
            else:
                response = get_error_resp(json)

        _filename_chunk = re.search(r'-(.+\.\w+$)', item['filename'])
        if _filename_chunk:
            filename_chunk = _filename_chunk[1]
        else:
            filename_chunk = f"{report_type}.json"

        responses[filename_chunk] = response

    return responses


def save_3s_report(src, report_date, bucket_name=S3_BUCKET, **kwargs):
    report_type = kwargs["report_type"]
    config = REPORT_CONFIGS[report_type]
    filename = config["filename"].format(report_date)

    try:
        bucket = s3_resource.Bucket(bucket_name)
    except ClientError as e:
        response = get_error_resp(e)
        bucket = None

    try:
        head = s3_client.head_object(Bucket=bucket_name, Key=filename)
    except ClientError:
        etag = ""
    else:
        etag = head.get("ETag", "").strip('"')

    try:
        s3_obj = bucket.Object(filename)
    except ClientError as e:
        response = get_error_resp(e)
        s3_obj = None
    except AttributeError:
        s3_obj = None

    if etag and not kwargs.get("overwrite"):
        response = get_error_resp(f"{filename} already exists!")
        s3_obj = None

    try:
        s3_obj.upload_fileobj(src)
    except ClientError as e:
        response = get_error_resp(e)
    except AttributeError:
        pass
    else:
        try:
            s3_obj.wait_until_exists(IfNoneMatch=etag)
        except WaiterError as e:
            not_modified = "Not Modified" in str(e)
            status_code = 304 if not_modified else 500
            response = get_error_resp(e, status_code)
        else:
            head = s3_client.head_object(Bucket=bucket_name, Key=filename)
            meta = head["ResponseMetadata"]
            headers = meta["HTTPHeaders"]

            response = {
                "ok": True,
                "message": f"Successfully saved {filename}!",
                "last_modified": headers.get("last-modified"),
                "etag": headers.get("etag", "").strip('"'),
                "content_length": headers.get("content-length"),
                "status_code": meta.get("HTTPStatusCode"),
            }

    return response


def save_local_report(src, report_date, report_type=None, **kwargs):
    config = REPORT_CONFIGS[report_type]
    filename = config["filename"].format(report_date)

    with open(filename, mode="wb") as dest:
        try:
            copyfileobj(src, dest)
        except Exception as e:
            response = get_error_resp(e)
        else:
            new_pos = dest.tell()
            response = {
                "ok": bool(new_pos),
                "message": f"Wrote {new_pos} bytes to {filename}!",
                "content_length": new_pos,
                "status_code": 200,
            }

    return response


def get_3s_report(report_date, bucket_name=S3_BUCKET, **kwargs):
    f = BytesIO()
    report_type = kwargs["report_type"]
    config = REPORT_CONFIGS[report_type]
    filename = config["filename"].format(report_date)

    try:
        s3_client.download_fileobj(bucket_name, filename, f)
    except ClientError as e:
        resp = {}
    else:
        f.seek(0)

        try:
            resp = load(f)
        except JSONDecodeError as e:
            resp = {}

    return resp


def parse_idph_county_report(requested_date, last_updated, **json):
    config = REPORT_CONFIGS["county"]
    date_format = config["date_format"]

    historical_county_values = json["historical_county"]["values"]
    test_dates = sorted(v["testDate"] for v in historical_county_values)
    earliest_updated = dt.strptime(test_dates[0], date_format)

    if last_updated >= requested_date >= earliest_updated:
        padded_date_key = requested_date.strftime(date_format)
        date_key = padded_date_key.lstrip("0").replace("/0", "/")
        historical_county = {
            v["testDate"]: v["values"] for v in historical_county_values
        }
        state_filter = lambda x: True if 'testDate' in x else False
        state_testing_results = filter(state_filter, json["state_testing_results"]["values"])
        historical_state = {
            v["testDate"]: v for v in state_testing_results
        }
        county = historical_county.get(date_key, {})
        state = historical_state.get(date_key, {})

        if requested_date == last_updated:
            demographics = json["demographics"]
        else:
            demographics = {}

        resp = {"county": county, "state": state, "demographics": demographics}
    else:
        resp = {}

    return resp


def parse_idph_hospital_report(requested_date, last_updated, **json):
    config = REPORT_CONFIGS["hospital"]
    date_format = config["date_format"]

    historical_hospital_values = json["HospitalUtilizationResults"]
    report_dates = sorted(v["reportDate"] for v in historical_hospital_values)
    earliest_updated = dt.strptime(report_dates[0], date_format)
    date_key = requested_date.strftime(date_format)

    if last_updated >= requested_date >= earliest_updated:
        if requested_date == last_updated:
            state = json["statewideValues"]
            state["reportDate"] = date_key
            region = json["regionValues"]
        else:
            historical_hospital = {
                v["reportDate"]: v for v in historical_hospital_values
            }
            state = historical_hospital.get(date_key, {})
            region = {}

        resp = {"state": state, "region": region}
    else:
        resp = {}

    return resp


def parse_idph_zip_report(requested_date, last_updated, **json):
    if last_updated == requested_date:
        resp = {"zipcodes": json["zip_values"]}
    else:
        resp = {}

    return resp


PARSERS = {
    "county": parse_idph_county_report,
    "zip": parse_idph_zip_report,
    "hospital": parse_idph_hospital_report,
}


def get_idph_report(report_date, **kwargs):
    report_type = kwargs["report_type"]
    config = REPORT_CONFIGS[report_type]
    report_name = config["report_name"]
    r = requests.get(BASE_URL.format(report_name))

    try:
        json = r.json()
    except JSONDecodeError:
        resp = {}
    else:
        requested_date = dt.strptime(report_date, S3_DATE_FORMAT)
        last_updated = dt(**json["LastUpdateDate"])
        parse = PARSERS[report_type]
        resp = parse(requested_date, last_updated, **json)

    return resp


REPORT_FUNCS = {
    (True, "get", "county"): partial(get_3s_report, report_type="county"),
    (True, "get", "zip"): partial(get_3s_report, report_type="zip"),
    (True, "get", "hospital"): partial(get_3s_report, report_type="hospital"),
    (False, "get", "county"): partial(get_idph_report, report_type="county"),
    (False, "get", "zip"): partial(get_idph_report, report_type="zip"),
    (False, "get", "hospital"): partial(get_idph_report, report_type="hospital"),
    ('s3', "save", "county"): partial(save_3s_report, report_type="county"),
    ('s3', "save", "zip"): partial(save_3s_report, report_type="zip"),
    ('s3', "save", "hospital"): partial(save_3s_report, report_type="hospital"),
    ('local', "save", "county"): partial(save_local_report, report_type="county"),
    ('local', "save", "zip"): partial(save_local_report, report_type="zip"),
    ('local', "save", "hospital"): partial(save_local_report, report_type="hospital"),
    ('ckan', "save", "county"): partial(save_ckan_report, report_type="county"),
    ('ckan', "save", "zip"): partial(save_ckan_report, report_type="zip"),
    ('ckan', "save", "hospital"): partial(save_ckan_report, report_type="hospital"),
}


def get_report(report_date, report_type="county", use_s3=False, **kwargs):
    report_func = REPORT_FUNCS[(use_s3, "get", report_type)]
    return report_func(report_date, **kwargs)


def save_report(report, report_date, report_type="county", **kwargs):
    src = BytesIO()

    if report:
        options = {"indent": 2, "sort_keys": True, "ensure_ascii": False}

        try:
            data = dumps(report, **options)
        except Exception as e:
            response = get_error_resp(e)
        else:
            response = get_error_resp(f"No data written for {report_date}.", 404)
            src.write(data.encode("utf-8"))
    else:
        response = get_error_resp(f"No data found for {report_date}.", 404)

    src_pos = src.tell()
    src.seek(0)

    if src_pos:
        dest = kwargs.get('dest', 'local')
        src = data if dest == 'ckan' else src
        report_func = REPORT_FUNCS[(dest, "save", report_type)]
        response = report_func(src, report_date, **kwargs)

    return response


def save_report_by_id(job_id, report_date, **kwargs):
    job = q.fetch_job(job_id)

    if job and job.get_status() == "finished":
        response = save_report(job.result, report_date, **kwargs)
    else:
        response = get_job_result(job)

    return response


def enqueue_work(report_date, **kwargs):
    _job = q.enqueue(get_report, report_date, **kwargs)
    job = q.enqueue(save_report_by_id, _job.id, report_date, depends_on=_job, **kwargs)
    return get_job_result(job)


def fetch_report(report_date, enqueue=False, **kwargs):
    if enqueue:
        response = enqueue_work(report_date, **kwargs)
    else:
        # TODO: Possibly change get_report
        get_from_s3 = kwargs.get("source") == "s3"

        report = get_report(report_date, use_s3=get_from_s3, **kwargs)
        return save_report(report, report_date, **kwargs)


def load_report(report_date, enqueue=False, **kwargs):
    if enqueue:
        job = q.enqueue(get_report, report_date, use_s3=True, **kwargs)
        resp = get_job_result(job)
        json = resp["result"]
    else:
        json = get_report(report_date, use_s3=True, **kwargs)

    if json:
        response = {
            "message": f"Successfully found data for date {report_date}!",
            "result": json,
        }
    else:
        response = get_error_resp(f"No data found for date {report_date}.")

    return response


def get_status(use_s3=True, **kwargs):
    if use_s3:
        _last_updated = _get_last_update_date(use_s3=use_s3, **kwargs)
        last_updated = _last_updated.strftime("%A %b %d, %Y")
        dataset_age = (dt.today() - _last_updated).days
        status_code = 200 if dataset_age < 2 else 500
        days = p.plural("day", dataset_age)

        response = {
            "ok": status_code == 200,
            "message": f"Last updated on {last_updated} ({dataset_age} {days} ago).",
            "result": {"last_updated": last_updated, "dataset_age": dataset_age},
            "status_code": status_code,
        }
    else:
        response = {"ok": False, "message": "Not implemented!", "status_code": 501}

    return response


###########################################################################
# ROUTES
###########################################################################
@blueprint.route("/")
def root():
    return redirect(url_for(".home"))


@blueprint.route(PREFIX)
@cache_header(ROUTE_TIMEOUT)
def home():
    response = {
        "description": "Returns API documentation",
        "message": "Welcome to the Alegna Commission Calculator API!",
        "links": get_links(app.url_map.iter_rules()),
    }

    return jsonify(**response)


@blueprint.route(f"{PREFIX}/status")
def status():
    kwargs = parse_kwargs(app)
    use_s3 = kwargs.get("source", "s3") == "s3"
    response = get_status(use_s3=use_s3)
    response["description"] = "Displays the status of the s3 bucket"
    response["links"] = get_links(app.url_map.iter_rules())
    return jsonify(**response)


@blueprint.route(f"{PREFIX}/ipsum")
@cache_header(ROUTE_TIMEOUT, key_prefix="%s")
def ipsum():
    response = {
        "description": "Displays a random sentence",
        "links": get_links(app.url_map.iter_rules()),
        "message": fake.sentence(),
    }

    return jsonify(**response)


@blueprint.route(f"{PREFIX}/result/<string:job_id>")
def result(job_id):
    """ Displays a job result.

    Args:
        job_id (str): The job id.
    """
    response = get_job_result_by_id(job_id)
    response["links"] = get_links(app.url_map.iter_rules())
    return jsonify(**response)


###########################################################################
# METHODVIEW ROUTES
###########################################################################
class Report(MethodView):
    def __init__(self):
        """ Reports

        Kwargs:
            date (str): Date of the report to save.
        """
        self.kwargs = parse_kwargs(app)
        end = self.kwargs.get("end", date.today().strftime(S3_DATE_FORMAT))
        self.end_date = dt.strptime(end, S3_DATE_FORMAT)
        self.days = self.kwargs.get("days", DAYS)

    def post(self):
        """ Saves new reports
        """
        result = {}

        for day in range(self.days):
            start_date = self.end_date - timedelta(days=day)
            report_date = start_date.strftime(S3_DATE_FORMAT)
            json = fetch_report(report_date, **self.kwargs)
            result[report_date] = {**json}

        response = {'result': result}
        return jsonify(**response)

    def get(self):
        """ Retrieves saved reports
        """
        result = {}

        for day in range(self.days):
            start_date = self.end_date - timedelta(days=day)
            report_date = start_date.strftime(S3_DATE_FORMAT)
            response = load_report(report_date, **self.kwargs)
            json = response.get("result")

            if json:
                result[report_date] = json

        response["result"] = result
        return jsonify(**response)


class Memoization(MethodView):
    def get(self):
        base_url = get_request_base()

        response = {
            "description": "Deletes a cache url",
            "links": get_links(app.url_map.iter_rules()),
            "message": f"The {request.method}:{base_url} route is not yet complete.",
        }

        return jsonify(**response)

    def delete(self, path=None):
        if path:
            url = f"{PREFIX}/{path}"
            cache.delete(url)
            message = f"Deleted cache for {url}"
        else:
            cache.clear()
            message = "Caches cleared!"

        response = {"links": get_links(app.url_map.iter_rules()), "message": message}
        return jsonify(**response)


add_rule = blueprint.add_url_rule

method_views = {
    "memoization": {
        "view": Memoization,
        "param": "string:path",
        "methods": ["GET", "DELETE"],
    },
    "report": {"view": Report, "methods": ["GET", "POST"]},
}

for name, options in method_views.items():
    if options.get("add_prefixes"):
        prefixes = Config.API_PREFIXES
    else:
        prefixes = [None]

    for prefix in prefixes:
        if prefix:
            route_name = f"{prefix}-{name}".lower()
        else:
            route_name = name

        view_func = options["view"].as_view(route_name)
        methods = options.get("methods")
        url = f"{PREFIX}/{route_name}"

        if options.get("param"):
            param = options["param"]
            url += f"/<{param}>"

        add_rule(url, view_func=view_func, methods=methods)
