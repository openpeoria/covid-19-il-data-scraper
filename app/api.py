# -*- coding: utf-8 -*-
""" app.api
~~~~~~~~~~~~
Provides endpoints for authenticating with and pulling data from quickbooks.

Live Site:
    https://alegna-api.nerevu.com/v1

Endpoints:
    Visit the live site for a list of all available endpoints
"""
import random

from json import dumps
from json.decoder import JSONDecodeError
from urllib.parse import parse_qsl, urlparse
from io import BytesIO
from shutil import copyfileobj
from datetime import date, timedelta

import requests
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

from config import Config
from app import cache
from app.utils import (
    responsify,
    jsonify,
    parse,
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
blueprint = Blueprint("API", __name__)
headers = {"Accept": "application/json"}
fake = Faker()

try:
    session = boto3.Session(profile_name="nerevu")
except ProfileNotFound:
    botoKwargs = {
        "aws_access_key_id": Config.AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": Config.AWS_SECRET_ACCESS_KEY,
        "region_name": Config.AWS_REGION,
    }

    session = boto3.Session(**botoKwargs)

s3_client = session.client("s3")
s3_resource = session.resource("s3")


logger = gogo.Gogo(__name__, monolog=True).logger

# these don't change based on mode, so no need to do app.config['...']
PREFIX = Config.API_URL_PREFIX
HEADERS = {"Accept": "application/json"}
ROUTE_TIMEOUT = Config.ROUTE_TIMEOUT
SET_TIMEOUT = Config.SET_TIMEOUT
LRU_CACHE_SIZE = Config.LRU_CACHE_SIZE
KEY_WHITELIST = Config.KEY_WHITELIST
BASE_URL = Config.BASE_URL
S3_BUCKET = Config.S3_BUCKET

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


def save_report(report, report_date, use_s3=False, bucketName=S3_BUCKET, **kwargs):
    filename = f"IL_county_COVID19_data_{report_date}.json"
    options = {"indent": 2, "sort_keys": True, "ensure_ascii": False}
    src = BytesIO()

    if report:
        try:
            data = dumps(report, **options)
        except Exception as e:
            response = get_error_resp(e)
        else:
            src.write(data.encode("utf-8"))
    else:
        response = get_error_resp(f"No data found for {report_date}.", 404)

    src_pos = src.tell()
    src.seek(0)

    if src_pos and use_s3:
        try:
            bucket = s3_resource.Bucket(bucketName)
        except ClientError as e:
            response = get_error_resp(e)
            bucket = None

        try:
            head = s3_client.head_object(Bucket=bucketName, Key=filename)
        except ClientError:
            etag = ""
        else:
            etag = head["ETag"].strip('"')

        try:
            s3_obj = bucket.Object(filename)
        except ClientError as e:
            response = get_error_resp(e)
            s3_obj = None
        except AttributeError:
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
                head = s3_client.head_object(Bucket=bucketName, Key=filename)
                meta = head["ResponseMetadata"]

                response = {
                    "ok": True,
                    "message": f"Successfully saved {filename}!",
                    "last_modified": meta["last-modified"],
                    "etag": meta["etag"],
                    "content_length": meta["content-length"],
                    "status_code": meta["HTTPStatusCode"],
                }
    elif src_pos:
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


def get_report(report_date, **kwargs):
    filename = f"COVID19CountyResults{report_date}.json"
    url = f"{BASE_URL}/{filename}"
    r = requests.get(url)

    try:
        resp = r.json()
    except JSONDecodeError:
        resp = {}

    return resp


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
        report = get_report(report_date, **kwargs)
        response = save_report(report, report_date, **kwargs)

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
    def parse_kwargs(self):
        kwargs = {k: parse(v) for k, v in request.args.to_dict().items()}

        with app.app_context():
            for k, v in app.config.items():
                if k in KEY_WHITELIST:
                    kwargs.setdefault(k.lower(), v)

        return kwargs

    def post(self):
        """ Saves new reports

        Args:
            arg (str): Package id of the package to update.
        """
        response = fetch_report(date, **self.kwargs)
        return jsonify(**response)

    def get(self):
        """ Retrieves saved reports

        Args:
            arg (str): Package id of the package to update.
        """
        response = retrieve_report(date, **self.kwargs)
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
    }
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
