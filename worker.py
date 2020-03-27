# -*- coding: utf-8 -*-
"""
    app.worker
    ~~~~~~~~~~

    Provides the rq worker
"""
from app.connection import conn
from rq import Worker, Queue, Connection

listen = ['high', 'default', 'low']

if __name__ == '__main__':
    with Connection(conn):
        worker = Worker(map(Queue, listen))
        worker.work()
