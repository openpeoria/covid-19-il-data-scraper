# -*- coding: utf-8 -*-
""" app.api
~~~~~~~~~~~~

Live Site:

Endpoints:
"""
import re

from json import dumps, load, loads
from json.decoder import JSONDecodeError
from functools import partial
from io import BytesIO
from shutil import copyfileobj
from datetime import datetime as dt, date, timedelta
from itertools import groupby, chain

import requests
import inflect
import boto3

from botocore.exceptions import ClientError, WaiterError, ProfileNotFound

from faker import Faker
from flask import (
    Blueprint,
    current_app as app,
    request,
    url_for,
    redirect,
)

from flask.views import MethodView
from rq import Queue

from config import Config
from app import cache
from app.helpers import log
from app.utils import (
    jsonify,
    parse_kwargs,
    cache_header,
    get_request_base,
    get_links,
    CTYPES,
)
from app.connection import conn

from meza import process as pr
from meza.convert import records2csv
from meza.fntools import dfilter
from riko.dotdict import DotDict

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

# these don't change based on mode, so no need to do app.config['...']
PREFIX = Config.API_URL_PREFIX
HEADERS = {"Accept": "application/json"}
ROUTE_TIMEOUT = Config.ROUTE_TIMEOUT
SET_TIMEOUT = Config.SET_TIMEOUT
LRU_CACHE_SIZE = Config.LRU_CACHE_SIZE
BASE_URL = Config.BASE_URL
S3_BUCKET = Config.S3_BUCKET
S3_DATE_FORMAT = Config.S3_DATE_FORMAT
REPORTS = Config.REPORTS
CKAN_API_BASE_URL = Config.CKAN_API_BASE_URL
CKAN_HEADERS = {"X-CKAN-API-Key": Config.CKAN_API_KEY}

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
    config = REPORTS[report_type]
    prefix = config["basename"].split("{}")[0]

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


def get_response(msg, status_code=500):
    return {
        "ok": status_code < 400,
        "message": str(msg),
        "status_code": status_code,
    }


def flatten(record, *keys, sep="-"):
    if isinstance(record, list):
        for pos, r in enumerate(record):
            yield from flatten(r, *keys, str(pos), sep=sep)
    elif isinstance(record, dict):
        for k, v in record.items():
            yield from flatten(v, *keys, k, sep=sep)
    else:
        yield (sep.join(keys), record)


def gen_records(src, *paths, report_date=None, blacklist=None, **kwargs):
    data = DotDict(loads(src))
    change = kwargs.get("change") or {}
    nested_path = kwargs.get("nested_path", "")
    report_datetime = dt.strptime(report_date, S3_DATE_FORMAT)

    try:
        path, subpath = paths
    except ValueError:
        path, subpath = paths[0], None

    records = data.get(path, [])

    if records and kwargs.get("listize"):
        records = [records]

    for record in records:
        record["date"] = report_datetime.isoformat()

        if subpath:
            if "." in subpath:
                subpath_0, subpath_1 = subpath.split(".", maxsplit=1)
                reference_record = dfilter(record, blacklist + [subpath_0])
            else:
                reference_record = dfilter(record, blacklist + [subpath])

            for new_record in DotDict(record)[subpath]:
                combined = {**new_record, **reference_record}
                clean_record = dfilter(combined, blacklist)
                yield {change.get(k, k): v for k, v in clean_record.items()}
        elif nested_path:
            # key is like 'race-7-description'
            keyfunc = lambda x: "-".join(re.findall(r"\d+", x[0]))
            reference_record = dfilter(record, [nested_path])
            nested = record.get(nested_path)

            if nested:
                flattened = flatten(nested)

                for key, group in groupby(flattened, keyfunc):
                    new_record = {re.sub(r"\d+-", "", k): v for k, v in group}
                    combined = {**new_record, **reference_record}
                    clean_record = dfilter(combined, blacklist)
                    yield {change.get(k, k): v for k, v in clean_record.items()}
        else:
            clean_record = dfilter(record, blacklist)
            yield {change.get(k, k): v for k, v in clean_record.items()}


def gen_new_records(src, report_date, report_type=None, **kwargs):
    config = REPORTS[report_type]
    global_blacklist = config.get("blacklist") or []

    for options in config["csv_options"]:
        blacklist = options.pop("blacklist", None) or []
        path = options["path"]
        ekwargs = {
            "report_date": report_date,
            "blacklist": blacklist + global_blacklist,
        }
        records = gen_records(src, *path.split(".[]."), **ekwargs, **options)
        records, peek = pr.peek(records)

        if peek:
            resource_id = options["resource_id"]
            package_id = options["package_id"]
            dates = get_ckan_report_dates_by_id(resource_id)
            new = [r for r in records if r.get("date") not in dates] if dates else None

            if new:
                yield (new, resource_id, package_id)


def gen_csvs(src, report_date, report_type=None, **kwargs):
    # The impedance mismatch (checking datastore but uploading to filestore) can
    # lead to a race condition when looping over multiple dates
    config = REPORTS[report_type]

    for options in config["csv_options"]:
        path = options["path"]
        paths = path.split(".[].")
        records = gen_records(src, *paths, report_date=report_date, **options)
        records, peek = pr.peek(records)

        if peek:
            resource_id = options["resource_id"]
            package_id = options["package_id"]
            # datastore_upsert would be ideal, but not sure how to set the `key` without
            # using datastore_create via API (as opposed to datapusher)
            # also not sure if filestore will reflect the datastore
            # docs.ckan.org/en/2.8/maintaining/datastore.html#ckanext.datastore.logic.action.datastore_upsert
            report = gen_ckan_records_by_id(resource_id, report_date)
            report, peek = pr.peek(report)
            new = chain(report, records) if peek else records

            try:
                yield (records2csv(new), resource_id, package_id)
            except Exception as e:
                app.logger.error(e)


def post_ckan_report(src, resource_id, package_id, filename=None, **kwargs):
    assert resource_id or filename
    response = None

    if filename:
        try:
            report = next(gen_ckan_reports(filename))
        except StopIteration:
            report = {}
        else:
            resource_id = report["id"]
            filename = report["name"]

        action = "updated" if report else "created"
        overwrite = kwargs.get("overwrite")
    else:
        action = "updated"
        overwrite = True

    is_update = action == "updated"

    if kwargs.get("datastore"):
        # docs.ckan.org/en/2.8/maintaining/datastore.html#ckanext.datastore.logic.action.datastore_upsert
        post_data = {
            "url": f"{CKAN_API_BASE_URL}/datastore_upsert",
            "json": {
                "resource_id": resource_id,
                "method": "insert",
                "records": src,
                "force": True,
            },
        }
    elif overwrite and is_update:
        # docs.ckan.org/en/2.8/api/#uploading-a-new-version-of-a-resource-file
        # docs.ckan.org/en/2.8/maintaining/filestore.html#filestore-api
        post_data = {
            "url": f"{CKAN_API_BASE_URL}/resource_update",
            "data": {"id": resource_id},
            "files": [("upload", src)],
        }
    elif is_update:
        response = get_response(f"{filename} already exists!", 304)
    else:
        # setup post_data for creating new resource
        # docs.ckan.org/en/2.8/api/index.html#ckan.logic.action.create.resource_create
        # docs.ckan.org/en/2.8/maintaining/filestore.html#filestore-api
        post_data = {
            "url": f"{CKAN_API_BASE_URL}/resource_create",
            "data": {
                "package_id": package_id,
                "mimetype": kwargs["mimetype"],
                "format": kwargs["mimetype"],
                "name": filename,
            },
            "files": [("upload", src)],
        }

    if not response:
        r = requests.post(headers=CKAN_HEADERS, **post_data)

        try:
            json = r.json()
        except JSONDecodeError:
            json = {}

        if r.ok:
            result = json["result"]

            response = {
                "ok": True,
                "message": f"Successfully {action} {filename or resource_id}!",
                "last_modified": result.get("last_modified"),
                "content_length": result.get("content_length"),
                "status_code": r.status_code,
            }
        else:
            error = json.get("error", r.text)
            response = get_response(error)

    return response


def post_ckan_reports(src, report_date, **kwargs):
    mimetype = CTYPES["csv"]
    report_type = kwargs["report_type"]
    config = REPORTS[report_type]
    report_phrase = get_report_phrase(report_date, report_type)

    if kwargs.get("datastore"):
        sources = gen_new_records(src, report_date, **kwargs)
    else:
        sources = gen_csvs(src, report_date, **kwargs)

    responses = [
        post_ckan_report(*args, mimetype=mimetype, **kwargs, **config)
        for args in sources
    ]

    ok = responses and all(r["ok"] for r in responses)

    if ok:
        message = f"Successfully posted data {report_phrase}!"
        status_code = 200
    elif responses:
        message = f"Error(s) encountered posting data {report_phrase}!"
        status_code = 500

        for response in responses:
            if not response["ok"]:
                log(**response)
    else:
        ok = True
        message = f"No data posted {report_phrase}!"
        status_code = 304

    return {
        "ok": ok,
        "message": message,
        "result": responses,
        "status_code": status_code,
    }


def post_3s_report(src, report_date, bucket_name=S3_BUCKET, **kwargs):
    report_type = kwargs["report_type"]
    config = REPORTS[report_type]
    basename = config["basename"].format(report_date)
    filename = f"{basename}.json"

    try:
        bucket = s3_resource.Bucket(bucket_name)
    except ClientError as e:
        response = get_response(e)
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
        response = get_response(e)
        s3_obj = None
    except AttributeError:
        s3_obj = None

    if etag and not kwargs.get("overwrite"):
        response = get_response(f"{filename} already exists!", 304)
        s3_obj = None

    try:
        s3_obj.upload_fileobj(src)
    except ClientError as e:
        response = get_response(e)
    except AttributeError:
        pass
    else:
        try:
            s3_obj.wait_until_exists(IfNoneMatch=etag)
        except WaiterError as e:
            not_modified = "Not Modified" in str(e)
            status_code = 304 if not_modified else 500
            response = get_response(e, status_code)
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


def post_local_report(src, report_date, report_type=None, **kwargs):
    config = REPORTS[report_type]
    basename = config["basename"].format(report_date)
    filename = f"{basename}.json"

    with open(filename, mode="wb") as dest:
        try:
            copyfileobj(src, dest)
        except Exception as e:
            response = get_response(e)
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
    config = REPORTS[report_type]
    basename = config["basename"].format(report_date)
    filename = f"{basename}.json"

    try:
        s3_client.download_fileobj(bucket_name, filename, f)
    except ClientError:
        report = {}
    else:
        f.seek(0)

        try:
            report = load(f)
        except JSONDecodeError:
            report = {}

    return report


def parse_idph_county_report(requested_date, last_updated, **json):
    config = REPORTS["county"]
    date_format = config["date_format"]

    historical_county_values = json["historical_county"]["values"]
    dated_historical_county_values = [
        v for v in historical_county_values if "testDate" in v
    ]

    state_testing_values = json["state_testing_results"]["values"]
    test_dates = sorted(v["testDate"] for v in dated_historical_county_values)
    earliest_updated = dt.strptime(test_dates[0], date_format)

    if last_updated >= requested_date >= earliest_updated:
        padded_date_key = requested_date.strftime(date_format)
        date_key = padded_date_key.lstrip("0").replace("/0", "/")
        historical_county = {
            v["testDate"]: v["values"]
            for v in dated_historical_county_values
            if v.get("values")
        }

        historical_state = {
            v["testDate"]: v for v in state_testing_values if "testDate" in v
        }

        county = historical_county.get(date_key, {})
        state = historical_state.get(date_key, {})

        if requested_date == last_updated:
            demographics = json["demographics"]
        else:
            demographics = {}

        report = {"county": county, "state": state, "demographics": demographics}
    else:
        report = {}

    return report


def parse_idph_hospital_report(requested_date, last_updated, **json):
    config = REPORTS["hospital"]
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

        report = {"state": state, "region": region}
    else:
        report = {}

    return report


def parse_idph_zip_report(requested_date, last_updated, **json):
    return {"zipcodes": json["zip_values"]} if last_updated == requested_date else {}


PARSERS = {
    "county": parse_idph_county_report,
    "zip": parse_idph_zip_report,
    "hospital": parse_idph_hospital_report,
}


# TODO: change report to report_id
def delete_ckan_report(report_date, report, report_type, **kwargs):
    r = requests.post(
        f"{CKAN_API_BASE_URL}/resource_delete",
        data={"id": report["id"]},
        headers=CKAN_HEADERS,
    )

    report_name = report["name"]

    if r.ok:
        response = {
            "ok": True,
            "message": f"Successfully deleted {report_name}!",
            "last_modified": report.get("last_modified"),
            "etag": report.get("name", "").strip('"'),
            "content_length": report.get("content_length"),
            "status_code": r.status_code,
        }
    else:
        try:
            error = r.json()["error"]
        except JSONDecodeError:
            error = r.text

        message = f"Failed to delete file {report_name} from ckan. ERROR: {error}"
        response = get_response(message)

    return response


def gen_ckan_reports(name):
    # resource_search by name matches any files that contain the query string
    # in the file name (not an exact match)
    r = requests.get(
        f"{CKAN_API_BASE_URL}/resource_search",
        params={"query": f"name:{name}"},
        headers=CKAN_HEADERS,
    )

    try:
        json = r.json()
    except JSONDecodeError:
        pass
    else:
        yield from json.get("result", {}).get("results", [])


def get_ckan_report_dates_by_id(resource_id):
    r = requests.get(
        f"{CKAN_API_BASE_URL}/datastore_search_sql",
        params={"sql": f'SELECT DISTINCT date FROM "{resource_id}";'},
        headers=CKAN_HEADERS,
    )

    try:
        json = r.json()
    except JSONDecodeError:
        json = {}

    if r.ok:
        records = json["result"]["records"]
        return {record["date"] for record in records}
    else:
        # {'__type': 'Validation Error', 'resource_id': ['Missing value']}
        error = json.get("error", {})
        err_msg = ", ".join(f"{k}: {v}" for k, v in error.items() if k != "__type")
        app.logger.error(err_msg)


def gen_ckan_records_by_id(resource_id, exclude_date, limit=4096, offset=0):
    r = requests.get(
        f"{CKAN_API_BASE_URL}/datastore_search",
        params={"resource_id": resource_id, "limit": limit, "offset": offset},
        headers=CKAN_HEADERS,
    )

    try:
        json = r.json()
    except JSONDecodeError:
        json = {}

    if r.ok:
        # datastore converts date strings to datetime, so just go with it
        exclude_datetime = dt.strptime(exclude_date, S3_DATE_FORMAT)
        exclude_datetime_iso = exclude_datetime.isoformat()
        result = json["result"]

        for record in result["records"]:
            if record.get("date") != exclude_datetime_iso:
                yield dfilter(record, ["_id"])

        total = result["total"]
        offset += limit

        if total > offset:
            yield from gen_ckan_records_by_id(resource_id, exclude_date, limit, offset)
    else:
        # {'__type': 'Validation Error', 'resource_id': ['Missing value']}
        error = json.get("error", {})
        err_msg = ", ".join(f"{k}: {v}" for k, v in error.items() if k != "__type")
        app.logger.error(err_msg)


def get_ckan_reports(report_date, report_type="", **kwargs):
    config = REPORTS[report_type]
    basename = config["basename"].format(report_date)
    return list(gen_ckan_reports(basename))


def get_idph_report(report_date, **kwargs):
    report_type = kwargs["report_type"]
    config = REPORTS[report_type]
    report_name = config["report_name"]
    r = requests.get(BASE_URL.format(report_name))

    try:
        json = r.json()
    except JSONDecodeError:
        report = {}
    else:
        requested_date = dt.strptime(report_date, S3_DATE_FORMAT)
        last_updated = dt(**json["LastUpdateDate"])
        parse = PARSERS[report_type]
        report = parse(requested_date, last_updated, **json)

    return report


REPORT_FUNCS = {
    ("s3", "get", "county"): partial(get_3s_report, report_type="county"),
    ("s3", "get", "zip"): partial(get_3s_report, report_type="zip"),
    ("s3", "get", "hospital"): partial(get_3s_report, report_type="hospital"),
    ("idph", "get", "county"): partial(get_idph_report, report_type="county"),
    ("idph", "get", "zip"): partial(get_idph_report, report_type="zip"),
    ("idph", "get", "hospital"): partial(get_idph_report, report_type="hospital"),
    ("ckan", "get", "county"): partial(get_ckan_reports, report_type="county"),
    ("ckan", "get", "zip"): partial(get_ckan_reports, report_type="zip"),
    ("ckan", "get", "hospital"): partial(get_ckan_reports, report_type="hospital"),
    ("s3", "post", "county"): partial(post_3s_report, report_type="county"),
    ("s3", "post", "zip"): partial(post_3s_report, report_type="zip"),
    ("s3", "post", "hospital"): partial(post_3s_report, report_type="hospital"),
    ("local", "post", "county"): partial(post_local_report, report_type="county"),
    ("local", "post", "zip"): partial(post_local_report, report_type="zip"),
    ("local", "post", "hospital"): partial(post_local_report, report_type="hospital"),
    ("ckan", "post", "county"): partial(post_ckan_reports, report_type="county"),
    ("ckan", "post", "zip"): partial(post_ckan_reports, report_type="zip"),
    ("ckan", "post", "hospital"): partial(post_ckan_reports, report_type="hospital"),
    ("ckan", "delete", "county"): partial(delete_ckan_report, report_type="county"),
    ("ckan", "delete", "zip"): partial(delete_ckan_report, report_type="zip"),
    ("ckan", "delete", "hospital"): partial(delete_ckan_report, report_type="hospital"),
}


def get_report_phrase(report_date, report_type="county", **kwargs):
    return f"for {report_type} report on {report_date}"


# get_report function returns multiple reports when src='ckan'
def get_report(report_date, src="idph", report_type="county", **kwargs):
    report_func = REPORT_FUNCS[(src, "get", report_type)]
    return report_func(report_date, **kwargs)


def delete_report(report, report_date, report_type="county", src="idph", **kwargs):
    report_func = REPORT_FUNCS[(src, "delete", report_type)]
    return report_func(report_date, report, **kwargs)


def post_report(report, report_date, report_type="county", **kwargs):
    report_phrase = get_report_phrase(report_date, report_type)
    src = BytesIO()

    if report:
        options = {"indent": 2, "sort_keys": True, "ensure_ascii": False}

        try:
            data = dumps(report, **options)
        except Exception as e:
            response = get_response(e)
        else:
            message = f"No data written {report_phrase}."
            response = get_response(message, 404)
            src.write(data.encode("utf-8"))
    else:
        message = f"No data found {report_phrase}."
        response = get_response(message, 304)

    src_pos = src.tell()
    src.seek(0)

    if src_pos:
        dest = kwargs.get("dest", "local")
        src = data if dest == "ckan" else src
        report_func = REPORT_FUNCS[(dest, "post", report_type)]
        response = report_func(src, report_date, **kwargs)

    return response


def post_report_by_id(job_id, report_date, **kwargs):
    job = q.fetch_job(job_id)

    if job and job.get_status() == "finished":
        response = post_report(job.result, report_date, **kwargs)
    else:
        response = get_job_result(job)

    return response


def add_report(report_date, enqueue=False, source="idph", **kwargs):
    if enqueue:
        _job = q.enqueue(get_report, report_date, src=source, **kwargs)
        job = q.enqueue(
            post_report_by_id, _job.id, report_date, depends_on=_job, **kwargs
        )
        response = get_job_result(job)
    else:
        report = get_report(report_date, src=source, **kwargs)
        response = post_report(report, report_date, **kwargs)

    return response


def load_report(report_date, enqueue=False, source="s3", **kwargs):
    report_phrase = get_report_phrase(report_date, **kwargs)

    if enqueue:
        job = q.enqueue(get_report, report_date, src=source, **kwargs)
        _response = get_job_result(job)
        report = _response["result"]
    else:
        report = get_report(report_date, src=source, **kwargs)

    if report:
        response = {
            "message": f"Successfully found data {report_phrase}!",
            "result": report,
        }
    else:
        response = get_response(f"No data found {report_phrase}.", 404)

    return response


def remove_report(report_date, enqueue=False, source="idph", **kwargs):
    report_phrase = get_report_phrase(report_date, **kwargs)
    report = {} if enqueue else get_report(report_date, src=source, **kwargs)
    ckan_src = source == "ckan"

    if report and ckan_src:
        results = [delete_report(r, report_date, src=source, **kwargs) for r in report]
        ok = all(r["ok"] for r in results)

        if ok:
            message = f"Successfully removed data {report_phrase}!"
            status_code = 200
        else:
            message = f"Error(s) encountered removing data {report_phrase}!"
            status_code = 500

        response = {
            "ok": ok,
            "message": message,
            "result": {r["name"]: r for r in results},
            "status_code": status_code,
        }
    elif report:
        response = delete_report(report, report_date, src=source, **kwargs)
    elif enqueue:
        response = get_response("Enqueuing is not yet enabled for this action!", 501)
    else:
        response = get_response(f"No reports found {report_phrase}.", 404)

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
        response = get_response("Not implemented!", 501)

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
        end = str(self.kwargs.get("end", date.today().strftime(S3_DATE_FORMAT)))
        self.end_date = dt.strptime(end, S3_DATE_FORMAT)
        self.days = self.kwargs.get("days", DAYS)

    def post(self):
        """ Saves reports"""
        result = {}

        for day in range(self.days):
            start_date = self.end_date - timedelta(days=day)
            report_date = start_date.strftime(S3_DATE_FORMAT)
            _response = add_report(report_date, **self.kwargs)
            result[report_date] = {**_response}

        ok = result and all(r["ok"] for r in result.values())

        # TODO: figure out how to find which dates erred
        report_phrase = get_report_phrase(report_date, **self.kwargs)

        if ok:
            message = f"Successfully posted data {report_phrase}!"
            status_code = 200
        elif result:
            message = f"Error(s) encountered posting data {report_phrase}!"
            status_code = 500

        response = {
            "ok": ok,
            "message": message,
            "result": result,
            "status_code": status_code,
        }

        return jsonify(**response)

    def get(self):
        """ Retrieves reports"""
        result = {}

        for day in range(self.days):
            start_date = self.end_date - timedelta(days=day)
            report_date = start_date.strftime(S3_DATE_FORMAT)
            _response = load_report(report_date, **self.kwargs)
            _result = _response.get("result")

            if _result:
                result[report_date] = _result

        if result:
            message = f"Successfully got data {report_phrase}!"
            status_code = 200
        else:
            message = f"Error(s) encountered getting data {report_phrase}!"
            status_code = 500

        response = {
            "ok": bool(result),
            "message": message,
            "result": result,
            "status_code": status_code,
        }

        return jsonify(**response)

    def delete(self):
        """ Removes reports """
        result = {}

        for day in range(self.days):
            start_date = self.end_date - timedelta(days=day)
            report_date = start_date.strftime(S3_DATE_FORMAT)
            _response = remove_report(report_date, **self.kwargs)
            result[report_date] = {**_response}

        ok = all(r["ok"] for r in result.values())

        if ok:
            message = f"Successfully deleted data {report_phrase}!"
            status_code = 200
        else:
            message = f"Error(s) encountered deleting data {report_phrase}!"
            status_code = 500

        response = {
            "ok": ok,
            "message": message,
            "result": result,
            "status_code": status_code,
        }

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
    "report": {"view": Report, "methods": ["GET", "POST", "DELETE"]},
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
