# -*- coding: utf-8 -*-
"""
    app.connection
    ~~~~~~~~~~~~~~

    Provides the redis connection
"""
import redis

from config import Config

conn = redis.from_url(Config.RQ_DASHBOARD_REDIS_URL)
