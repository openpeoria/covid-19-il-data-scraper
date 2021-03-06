# -*- coding: utf-8 -*-
"""
    config
    ~~~~~~
    Provides the flask config options
    ###########################################################################
    # WARNING: if running on a a staging server, you MUST set the 'STAGE' env
    # heroku config:set STAGE=true --remote staging
    #
    # WARNING (2): The heroku project must either have a postgres, memcache, or
    # redis db to be recognized as production. If it is not recognized as
    # production, Talisman will not be run.
    #
    # NOTE: To allow localhost to connect to QuickBooks, run
    # `manage -m Ngrok serve` in one terminal and
    # `ngrok http 5000 -subdomain=nerevu-api` in another terminal.
    ###########################################################################
"""
from os import getenv, urandom, path as p
from datetime import timedelta
from collections import namedtuple

from dotenv import load_dotenv

PARENT_DIR = p.abspath(p.dirname(__file__))

load_dotenv(p.join(PARENT_DIR, ".env"), override=True)
db_env_list = ["DATABASE_URL", "REDIS_URL", "MEMCACHIER_SERVERS", "REDISTOGO_URL"]

__USER__ = "reubano"
__APP_NAME__ = "covid19-il-data-api"
__PROD_SERVER__ = any(map(getenv, db_env_list))
__DEF_HOST__ = "127.0.0.1"
__DEF_REDIS_PORT__ = 6379
__DEF_REDIS_HOST__ = getenv("REDIS_PORT_6379_TCP_ADDR", __DEF_HOST__)
__DEF_REDIS_URL__ = "redis://{}:{}".format(__DEF_REDIS_HOST__, __DEF_REDIS_PORT__)

__STAG_SERVER__ = getenv("STAGE")
__END__ = "-stage" if __STAG_SERVER__ else ""
__SUB_DOMAIN__ = f"covid19-data-api{__END__}"
__AUTHOR__ = "Reuben Cummings"
__AUTHOR_EMAIL__ = "rcummings@nerevu.com"

DAYS_PER_MONTH = 30
SECRET_ENV = f"{__APP_NAME__}_SECRET".upper()
HEROKU_PR_NUMBER = getenv("HEROKU_PR_NUMBER")
HEROKU_TEST_RUN_ID = getenv("HEROKU_TEST_RUN_ID")

Admin = namedtuple("Admin", ["name", "email"])
get_path = lambda name: f"file://{p.join(PARENT_DIR, 'data', name)}"


def get_seconds(seconds=0, months=0, **kwargs):
    seconds = timedelta(seconds=seconds, **kwargs).total_seconds()

    if months:
        seconds += timedelta(DAYS_PER_MONTH).total_seconds() * months

    return int(seconds)


def get_server_name(heroku=False):
    if HEROKU_PR_NUMBER:
        DOMAIN = "herokuapp.com"
        HEROKU_APP_NAME = getenv("HEROKU_APP_NAME")
        SUB_DOMAIN = f"{HEROKU_APP_NAME}-pr-{HEROKU_PR_NUMBER}"
    elif heroku or HEROKU_TEST_RUN_ID:
        DOMAIN = "herokuapp.com"
        SUB_DOMAIN = f"nerevu-{__SUB_DOMAIN__}"
    else:
        DOMAIN = "nerevu.com"
        SUB_DOMAIN = __SUB_DOMAIN__

    return f"{SUB_DOMAIN}.{DOMAIN}"


class Config(object):
    DEBUG = False
    TESTING = False
    DEBUG_MEMCACHE = True
    DEBUG_QB_CLIENT = False
    PARALLEL = False
    OAUTHLIB_INSECURE_TRANSPORT = False
    PROD_SERVER = __PROD_SERVER__

    # see http://bootswatch.com/3/ for available swatches
    FLASK_ADMIN_SWATCH = "cerulean"
    ADMIN = Admin(__AUTHOR__, __AUTHOR_EMAIL__)
    ADMINS = frozenset([ADMIN.email])
    HOST = "127.0.0.1"

    # These don't change
    ROUTE_DEBOUNCE = get_seconds(5)
    ROUTE_TIMEOUT = get_seconds(0)
    SET_TIMEOUT = get_seconds(days=30)
    REPORT_MONTHS = 3
    LRU_CACHE_SIZE = 128
    REPORT_DAYS = REPORT_MONTHS * DAYS_PER_MONTH
    SEND_FILE_MAX_AGE_DEFAULT = ROUTE_TIMEOUT
    EMPTY_TIMEOUT = ROUTE_TIMEOUT * 10
    API_URL_PREFIX = "/v1"
    SECRET = getenv(SECRET_ENV, urandom(24))
    CHROME_DRIVER_VERSIONS = [None] + list(range(81, 77, -1))

    APP_CONFIG_WHITELIST = {
        "CHUNK_SIZE",
        "ROW_LIMIT",
        "ERR_LIMIT",
        "ADMIN",
        "SECRET",
    }

    # Variables warnings
    REQUIRED_SETTINGS = []
    OPTIONAL_SETTINGS = []
    REQUIRED_PROD_SETTINGS = [SECRET_ENV]

    # Logging
    MAILGUN_DOMAIN = getenv("MAILGUN_DOMAIN")
    MAILGUN_SMTP_PASSWORD = getenv("MAILGUN_SMTP_PASSWORD")
    REQUIRED_PROD_SETTINGS += ["MAILGUN_DOMAIN", "MAILGUN_SMTP_PASSWORD"]

    # Authentication
    AUTHENTICATION = {}

    # CKAN
    CKAN_API_KEY = getenv("CKAN_API_KEY")
    CKAN_API_BASE_URL = "https://data.openpeoria.com/api/3/action"
    REQUIRED_SETTINGS += ["CKAN_API_KEY"]

    # Data
    BASE_URL = "https://www.dph.illinois.gov/sitefiles/{}.json?nocache=1"
    REPORTS = {
        "county": {
            "report_name": "COVIDHistoricalTestResults",
            "basename": "IL_county_COVID19_data_{}",
            "date_format": "%m/%d/%Y",
            "blacklist": ["probable_deaths"],
            "csv_options": [
                {
                    "path": "county",
                    "resource_id": "4d0ed067-8f3d-4faa-8cf5-68bcf6225afd",
                    "package_id": "il-covid19-cases-county",
                },
                {
                    "path": "demographics.age",
                    "nested_path": "demographics",
                    "blacklist": ["count", "tested", "deaths", "race-color"],
                    "change": {
                        "race-count": "confirmed_cases",
                        "race-deaths": "deaths",
                        "race-description": "race",
                        "race-tested": "tested",
                    },
                    "resource_id": "051e216b-874d-48f3-8ef3-64573499f0c0",
                    "package_id": "il-covid19-cases-state",
                },
                {
                    "path": "demographics.gender",
                    "blacklist": ["color"],
                    "change": {"count": "confirmed_cases", "description": "gender"},
                    "resource_id": "3226e8f5-54c6-46cd-ad46-e91d6e6c6d7b",
                    "package_id": "il-covid19-cases-state",
                },
                {
                    "path": "demographics.race",
                    "blacklist": ["color"],
                    "change": {"count": "confirmed_cases", "description": "race"},
                    "resource_id": "190fd921-d0ab-4f3a-80a0-0854c6265cc0",
                    "package_id": "il-covid19-cases-state",
                },
            ],
        },
        "zip": {
            "report_name": "COVIDZip",
            "basename": "IL_zip_COVID19_data_{}",
            "blacklist": [],
            "csv_options": [
                {
                    "path": "zipcodes",
                    "blacklist": ["demographics"],
                    "change": {"zip": "zip_code"},
                    "resource_id": "05868207-2fed-40d2-a859-b9b2e98c77b0",
                    "package_id": "illinois-covid19-cases-zip-code",
                },
                {
                    "path": "zipcodes.[].demographics.age",
                    "blacklist": ["confirmed_cases", "total_tested", "demographics"],
                    "change": {"count": "confirmed_cases", "zip": "zip_code"},
                    "resource_id": "5b37b859-a08c-4af1-bc6f-dcc107e4b3a8",
                    "package_id": "illinois-covid19-cases-zip-code",
                },
                {
                    "path": "zipcodes.[].demographics.gender",
                    "blacklist": [
                        "confirmed_cases",
                        "total_tested",
                        "color",
                        "demographics",
                    ],
                    "change": {
                        "count": "confirmed_cases",
                        "zip": "zip_code",
                        "description": "gender",
                    },
                    "resource_id": "956a95da-71f4-4788-bc1e-2c924409402b",
                    "package_id": "illinois-covid19-cases-zip-code",
                },
                {
                    "path": "zipcodes.[].demographics.race",
                    "blacklist": [
                        "confirmed_cases",
                        "total_tested",
                        "color",
                        "demographics",
                    ],
                    "change": {
                        "count": "confirmed_cases",
                        "zip": "zip_code",
                        "description": "race",
                    },
                    "resource_id": "e586d10a-51ff-4ad1-af2d-2a5302ad497d",
                    "package_id": "illinois-covid19-cases-zip-code",
                },
            ],
        },
        "hospital": {
            "report_name": "COVIDHospitalRegions",
            "basename": "IL_regional_hospital_data_{}",
            "date_format": "%Y-%m-%d",
            "blacklist": [],
            "csv_options": [
                {
                    "path": "region",
                    "blacklist": ["id"],
                    "resource_id": "1d2052c1-1405-43fd-8fee-908e10884873",
                    "package_id": "illinois-regional-hospital-data",
                },
                {
                    "path": "state",
                    "listize": True,
                    "blacklist": [
                        "id",
                        "reportDate",
                        "TotalInUseBedsCOVID",
                        "ICUInUseBedsNonCOVID",
                        "VentilatorInUseNonCOVID",
                        "TotalInUseBedsNonCOVID",
                    ],
                    "change": {
                        "ICUInUseBedsCOVID": "ICUCovidPatients",
                        "ICUBeds": "ICUCapacity",
                        "VentilatorAvailable": "VentsAvailable",
                        "VentilatorInUseCOVID": "VentilatorsInUseCOVID",
                        "TotalOpenBeds": "TotalBedsAvailable",
                    },
                    "resource_id": "f679f8e2-90a0-451a-8808-89e5896a27c9",
                    "package_id": "illinois-regional-hospital-data",
                },
            ],
        },
    }

    S3_DATE_FORMAT = "%Y%m%d"
    DAYS = 7

    # AWS
    REQUIRED_PROD_SETTINGS += [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
    ]
    AWS_ACCESS_KEY_ID = getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = getenv("AWS_SECRET_ACCESS_KEY")
    AWS_REGION = getenv("AWS_REGION")
    S3_BUCKET = getenv("S3_BUCKET", "covid19-il")

    # RQ
    REQUIRED_PROD_SETTINGS += ["RQ_DASHBOARD_USERNAME", "RQ_DASHBOARD_PASSWORD"]
    RQ_DASHBOARD_REDIS_URL = (
        getenv("REDIS_URL") or getenv("REDISTOGO_URL") or __DEF_REDIS_URL__
    )
    RQ_DASHBOARD_DEBUG = False

    # APIs
    API_PREFIXES = []

    # Whitelist
    APP_CONFIG_WHITELIST.update(REQUIRED_SETTINGS)
    APP_CONFIG_WHITELIST.update(REQUIRED_PROD_SETTINGS)
    APP_CONFIG_WHITELIST.update(OPTIONAL_SETTINGS)

    # Change based on mode
    CACHE_DEFAULT_TIMEOUT = get_seconds(hours=24)
    CHUNK_SIZE = 256
    ROW_LIMIT = 32
    OAUTHLIB_INSECURE_TRANSPORT = False


class Production(Config):
    # TODO: setup nginx http://docs.gunicorn.org/en/latest/deploy.html
    #       or waitress https://github.com/etianen/django-herokuapp/issues/9
    #       test with slowloris https://github.com/gkbrk/slowloris
    #       look into preboot https://devcenter.heroku.com/articles/preboot
    defaultdb = f"postgres://{__USER__}@{__DEF_HOST__}/{__APP_NAME__.replace('-','_')}"
    SQLALCHEMY_DATABASE_URI = getenv("DATABASE_URL", defaultdb)

    # max 20 connections per dyno spread over 4 workers
    # look into a Null pool with pgbouncer
    # https://devcenter.heroku.com/articles/python-concurrency-and-database-connections
    SQLALCHEMY_POOL_SIZE = 3
    SQLALCHEMY_MAX_OVERFLOW = 2

    if __PROD_SERVER__:
        TALISMAN = True
        TALISMAN_FORCE_HTTPS_PERMANENT = True

        # https://stackoverflow.com/a/18428346/408556
        # https://github.com/Parallels/rq-dashboard/issues/328
        TALISMAN_CONTENT_SECURITY_POLICY = {
            "default-src": "'self'",
            "script-src": "'self' 'unsafe-inline' 'unsafe-eval'",
            "style-src": "'self' 'unsafe-inline'",
        }

    HOST = "0.0.0.0"


class Heroku(Production):
    server_name = get_server_name(True)
    API_URL = f"https://{server_name}{Config.API_URL_PREFIX}"

    if __PROD_SERVER__:
        SERVER_NAME = server_name


class Custom(Production):
    server_name = get_server_name()
    API_URL = f"https://{server_name}{Config.API_URL_PREFIX}"

    if __PROD_SERVER__:
        SERVER_NAME = server_name


class Development(Config):
    base = "sqlite:///{}?check_same_thread=False"
    ENV = "development"
    SQLALCHEMY_DATABASE_URI = base.format(p.join(PARENT_DIR, "app.db"))
    RQ_DASHBOARD_DEBUG = True
    DEBUG = True
    DEBUG_MEMCACHE = False
    DEBUG_QB_CLIENT = False
    CACHE_DEFAULT_TIMEOUT = get_seconds(hours=8)
    CHUNK_SIZE = 128
    ROW_LIMIT = 16
    SQLALCHEMY_TRACK_MODIFICATIONS = True
    OAUTHLIB_INSECURE_TRANSPORT = True


class Ngrok(Development):
    # Xero localhost callbacks work fine
    server_name = "nerevu-api.ngrok.io"
    API_URL = f"https://{server_name}{Config.API_URL_PREFIX}"


class Test(Config):
    ENV = "development"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    DEBUG = True
    DEBUG_MEMCACHE = False
    TESTING = True
    CACHE_DEFAULT_TIMEOUT = get_seconds(hours=1)
    CHUNK_SIZE = 64
    ROW_LIMIT = 8
    OAUTHLIB_INSECURE_TRANSPORT = True
