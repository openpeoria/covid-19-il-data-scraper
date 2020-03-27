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
from socket import error as SocketError, timeout as SocketTimeout
from pathlib import Path

import requests
import pygogo as gogo

from pkutils import parse_module
from meza.process import merge


PARENT_DIR = p.abspath(p.dirname(__file__))
DAYS_PER_MONTH = 30

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

Admin = namedtuple("Admin", ["name", "email"])
get_path = lambda name: f"file://{p.join(PARENT_DIR, 'data', name)}"
logger = gogo.Gogo(__name__, monolog=True).logger


def get_seconds(seconds=0, months=0, **kwargs):
    seconds = timedelta(seconds=seconds, **kwargs).total_seconds()

    if months:
        seconds += timedelta(DAYS_PER_MONTH).total_seconds() * months

    return int(seconds)


class Config(object):
    HEROKU = False
    DEBUG = False
    TESTING = False
    DEBUG_MEMCACHE = True
    PARALLEL = False
    PROD_SERVER = __PROD_SERVER__
    FLASK_ADMIN_SWATCH = "cerulean"  # http://bootswatch.com/3
    ADMIN = Admin(__AUTHOR__, __AUTHOR_EMAIL__)
    ADMINS = frozenset([ADMIN.email])
    HOST = __DEF_HOST__
    ROUTE_DEBOUNCE = get_seconds(5)
    ROUTE_TIMEOUT = get_seconds(hours=3)
    SET_TIMEOUT = get_seconds(days=30)
    LRU_CACHE_SIZE = 128
    SEND_FILE_MAX_AGE_DEFAULT = ROUTE_TIMEOUT
    EMPTY_TIMEOUT = ROUTE_TIMEOUT * 10
    API_URL_PREFIX = "/v1"
    SECRET_KEY = getenv("SECRET_KEY", urandom(24))
    KEY_WHITELIST = {"CHUNK_SIZE", "ROW_LIMIT", "ERR_LIMIT"}
    BASE_URL = "http://www.dph.illinois.gov/sites/default/files/COVID19"
    DATE_FORMAT = "%Y%m%d"

    # Variables warnings
    REQUIRED_SETTINGS = []
    REQUIRED_PROD_SETTINGS = []
    # RQ
    REQUIRED_PROD_SETTINGS += ["RQ_DASHBOARD_USERNAME", "RQ_DASHBOARD_PASSWORD"]
    RQ_DASHBOARD_REDIS_URL = (
        getenv("REDIS_URL") or getenv("REDISTOGO_URL") or __DEF_REDIS_URL__
    )
    RQ_DASHBOARD_USERNAME = getenv("RQ_DASHBOARD_USERNAME")
    RQ_DASHBOARD_PASSWORD = getenv("RQ_DASHBOARD_PASSWORD")
    RQ_DASHBOARD_DEBUG = False

    # APIs
    API_PREFIXES = []

    # Change based on mode
    CACHE_DEFAULT_TIMEOUT = get_seconds(hours=24)
    CHUNK_SIZE = 256
    ROW_LIMIT = 32
    OAUTHLIB_INSECURE_TRANSPORT = False


class Production(Config):
    # TODO: setup nginx http://docs.gunicorn.org/en/latest/deploy.html
    #  or waitress https://github.com/etianen/django-herokuapp/issues/9
    #  test with slowloris https://github.com/gkbrk/slowloris
    #  look into preboot https://devcenter.heroku.com/articles/preboot

    if __PROD_SERVER__:
        TALISMAN = True
        TALISMAN_FORCE_HTTPS_PERMANENT = True

        # https://stackoverflow.com/a/18428346/408556
        TALISMAN_CONTENT_SECURITY_POLICY = {
            "default-src": "'self'",
            "script-src": "'self' 'unsafe-inline' 'unsafe-eval'",
            "style-src": "'self' 'unsafe-inline'",
        }

    HOST = "0.0.0.0"


class Heroku(Production):
    HEROKU = True
    DOMAIN = "herokuapp.com"

    if __PROD_SERVER__:
        SERVER_NAME = f"{__SUB_DOMAIN__}.{DOMAIN}"
        logger.info(f"SERVER_NAME is {SERVER_NAME}")


class Custom(Production):
    DOMAIN = "nerevu.com"

    if __PROD_SERVER__:
        TALISMAN_SUBDOMAINS = True
        SERVER_NAME = f"{__SUB_DOMAIN__}.{DOMAIN}"
        logger.info(f"SERVER_NAME is {SERVER_NAME}")


class Development(Config):
    RQ_DASHBOARD_DEBUG = True
    DEBUG = True
    DEBUG_MEMCACHE = False
    CACHE_DEFAULT_TIMEOUT = get_seconds(hours=8)
    CHUNK_SIZE = 128
    OAUTHLIB_INSECURE_TRANSPORT = True


class Test(Config):
    DEBUG = True
    DEBUG_MEMCACHE = False
    TESTING = True
    CACHE_DEFAULT_TIMEOUT = get_seconds(hours=1)
    CHUNK_SIZE = 64
    OAUTHLIB_INSECURE_TRANSPORT = True
