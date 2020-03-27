# -*- coding: utf-8 -*-
"""
    app
    ~~~

    Provides the flask application
"""
from os import path
from functools import partial
from datetime import date

from flask import Flask, redirect, request
from flask_cors import CORS
from flask_caching import Cache
from flask_compress import Compress

import config
import pygogo as gogo

from mezmorize.utils import get_cache_config, get_cache_type
from rq_dashboard import default_settings
from rq_dashboard.cli import add_basic_auth
from mezmorize.utils import get_cache_config, get_cache_type
from meza.fntools import CustomEncoder

__version__ = "0.1.0"
__title__ = "COVID19 IL Data API"
__package_name__ = config.__APP_NAME__
__author__ = config.__AUTHOR__
__description__ = "COVID19 IL Data API"
__author_email__ = config.__AUTHOR_EMAIL__
__license__ = "MIT"
__copyright__ = "Copyright 2020 Nerevu Group"

cache = Cache()
compress = Compress()
cors = CORS()

logger = gogo.Gogo(__name__, monolog=True).logger


def create_app(config_mode=None, config_file=None):
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    cors.init_app(app)
    compress.init_app(app)

    required_setting_missing = False

    @app.before_request
    def clear_trailing():
        request_path = request.path

        if request_path != "/" and request_path.endswith("/"):
            return redirect(request_path[:-1])

    app.config.from_object(default_settings)

    if config_mode:
        app.config.from_object(getattr(config, config_mode))
    elif config_file:
        app.config.from_pyfile(config_file)
    else:
        app.config.from_envvar("APP_SETTINGS", silent=True)

    prefix = app.config.get("API_URL_PREFIX")
    server_name = app.config.get("SERVER_NAME")
    username = app.config.get("RQ_DASHBOARD_USERNAME")
    password = app.config.get("RQ_DASHBOARD_PASSWORD")

    for setting in app.config.get("REQUIRED_SETTINGS", []):
        if not app.config.get(setting):
            required_setting_missing = True
            logger.error(f"App setting {setting} is missing!")

    if app.config.get("PROD_SERVER"):
        if server_name:
            logger.info(f"SERVER_NAME is {server_name}.")
        else:
            logger.error(f"SERVER_NAME is not set!")

        for setting in app.config.get("REQUIRED_PROD_SETTINGS", []):
            if not app.config.get(setting):
                required_setting_missing = True
                logger.error(f"App setting {setting} is missing!")

    if not required_setting_missing:
        logger.info(f"All required app settings present!")

    app.register_blueprint(api)

    if username and password:
        add_basic_auth(blueprint=rq, username=username, password=password)
        logger.info(f"Creating RQ-dashboard login for {username}")

    app.register_blueprint(rq, url_prefix=f"{prefix}/dashboard")

    if app.config.get("TALISMAN"):
        from flask_talisman import Talisman

        talisman_kwargs = {
            k.replace("TALISMAN_", "").lower(): v
            for k, v in app.config.items()
            if k.startswith("TALISMAN_")
        }

        Talisman(app, **talisman_kwargs)

    if app.config.get("PROD_SERVER") or app.config.get("DEBUG_MEMCACHE"):
        cache_type = get_cache_type(spread=False)
    else:
        cache_type = "filesystem"

    logger.debug(f"Set cache type to {cache_type}")

    cache_config = get_cache_config(cache_type, **app.config)

    # TODO: keep until https://github.com/reubano/mezmorize/pull/1/files is
    # merged
    if cache_type == "filesystem" and not cache_config.get("CACHE_DIR"):
        cache_config["CACHE_DIR"] = path.join(
            path.abspath(path.dirname(__file__)), "cache"
        )

    cache.init_app(app, config=cache_config)

    # TODO: keep until https://github.com/sh4nks/flask-caching/issues/113 is
    # solved
    if "memcache" in cache_type:
        DEF_TIMEOUT = app.config.get("CACHE_DEFAULT_TIMEOUT")
        timeout = app.config.get("SET_TIMEOUT", DEF_TIMEOUT)
        cache.set = partial(cache.set, timeout=timeout)

    app.json_encoder = CustomEncoder
    return app


# put at bottom to avoid circular reference errors
from app.api import blueprint as api  # noqa
from rq_dashboard import blueprint as rq  # noqa
