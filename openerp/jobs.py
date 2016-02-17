#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# ---------------------------------------------------------------------
# celeryapp
# ---------------------------------------------------------------------
# Copyright (c) 2015, 2016 Merchise Autrement and Contributors
# All rights reserved.
#
# This is free software; you can redistribute it and/or modify it under the
# terms of the LICENCE attached (see LICENCE file) in the distribution
# package.
#
# Created on 2015-09-26

'''Odoo Celery Application.

Integrates Odoo and Celery, so that jobs can be started from the Odoo HTTP
workers and tasks can use the Odoo ORM.


'''

from __future__ import (division as _py3_division,
                        print_function as _py3_print,
                        absolute_import as _py3_abs_import)


from xoutil import logger  # noqa
from xoutil.context import context as _exec_context
from xoutil.objects import extract_attrs

from kombu import Exchange, Queue

import celery.exceptions
from celery import Celery as _CeleryApp

import openerp.tools.config as config

from openerp.release import version
from openerp.api import Environment
from openerp.modules.registry import RegistryManager


# The queues are named using the version info.  This is to avoid clashes with
# other systems using Celery and the same broker.  I've seen sentry tasks
# being routed to one of workers.

# TODO: Write an auto-migration of task routed to older queue names.

_VERSION_INFO = version.replace('.', '_').replace('+', '__')
ROUTE_NS = 'odoo-{}'.format(_VERSION_INFO)
ROUTE_KEY = '{}.#'.format(ROUTE_NS)

DEFAULT_QUEUE_NAME = '{}.default'.format(ROUTE_NS)
LOWPRI_QUEUE_NAME = '{}.low'.format(ROUTE_NS)
HIGHPRI_QUEUE_NAME = '{}.high'.format(ROUTE_NS)

del _VERSION_INFO


def _build_api_function(name, queue, **options):
    disallow_nested = not options.pop('allow_nested', False)

    def func(model, cr, uid, method, *args, **kwargs):
        args = _getargs(model, method, cr, uid, *args, **kwargs)
        if disallow_nested and CELERY_JOB in _exec_context:
            logger.warn('Nested background call detected for model %s '
                        'and method %s', model, method, extra=dict(
                            model=model, method=method, uid=uid,
                            args=args
                        ))
            return task(*args)
        else:
            return task.apply_async(queue=queue, args=args, **options)
    func.__name__ = name
    func.__doc__ = (
        '''Request to run a method in a celery worker.

        The job will be routed to the '{queue}' priority queue.

        :param model: The Odoo model.
        :param cr: The cursor or the DB name.
        :param uid: The user id.
        :param method: The name of method to run.

        :returns: An AsyncResult that represents the job.

        .. warning:: Nested calls don't issue sub-tasks.

           When running inside a background job, calling this function
           **won't** create another background job, but inline the function
           call.

        '''
    ).format(queue=queue.rsplit('.', 1)[-1] if '.' in queue else queue)
    return func


def DefaultDeferredType(**options):
    return _build_api_function('Deferred', DEFAULT_QUEUE_NAME, **options)


def HighPriorityDeferredType(**options):
    return _build_api_function('HighPriorityDeferred', HIGHPRI_QUEUE_NAME,
                               **options)


def LowPriorityDeferredType(**options):
    return _build_api_function('LowPriorityDeferred', LOWPRI_QUEUE_NAME,
                               **options)


Deferred = DefaultDeferredType()
HighPriorityDeferred = HighPriorityDeferredType()
LowPriorityDeferred = LowPriorityDeferredType()


def report_progress(message=None, progress=None, valuemin=None, valuemax=None,
                    status=None):
    '''Sends a progress notification to those waiting.

    :param message: The message to send to those waiting for the message.

    :param progress: A number in the range given by `range` indicating how
                     much has been done.

                     If you can't produce a good estimate is best to send
                     "stages" in the message.

    :param valuemin: The minimum value `progress` can take.

    :param valuemax: The maximum value `progress` can take.

    The `valuemin` and `valuemax` arguments must be reported together.  And
    once settle they cannot be changed.

    :param status: The reported status. This should be one of the strings
                   'success', 'failure' or 'pending'.

       .. warning:: This argument should not be used but for internal (job
                    framework module) purposes.

    '''
    _context = _exec_context[CELERY_JOB]
    job = _context.get('job')
    if job:
        if valuemin is None or valuemax is None:
            valuemin = valuemax = None
        elif valuemin >= valuemax:
            valuemin = valuemax = None
        _send(get_progress_channel(job), dict(
            status=status,
            message=message,
            progress=progress,
            valuemin=valuemin,
            valuemax=valuemax,
        ))


class Configuration(object):
    BROKER_URL = config.get('celery.broker', 'redis://localhost/9')
    # We don't use the backend to store results, but send results via another
    # message.  However to check the job status the backend is used.
    CELERY_RESULT_BACKEND = config.get('celery.backend', BROKER_URL)

    CELERY_DEFAULT_QUEUE = DEFAULT_QUEUE_NAME
    CELERY_DEFAULT_EXCHANGE_TYPE = 'direct'
    CELERY_DEFAULT_ROUTING_KEY = DEFAULT_QUEUE_NAME

    CELERY_SEND_EVENTS = True
    CELERYD_MAX_TASKS_PER_CHILD = 2000

    # TODO: Take queues from configuration.
    CELERY_QUEUES = (
        Queue(DEFAULT_QUEUE_NAME, Exchange(DEFAULT_QUEUE_NAME),
              routing_key=DEFAULT_QUEUE_NAME),
        Queue(LOWPRI_QUEUE_NAME, Exchange(LOWPRI_QUEUE_NAME),
              routing_key=LOWPRI_QUEUE_NAME),
        Queue(HIGHPRI_QUEUE_NAME, Exchange(HIGHPRI_QUEUE_NAME),
              routing_key=HIGHPRI_QUEUE_NAME),
    )
    CELERY_CREATE_MISSING_QUEUES = False

    CELERYD_TASK_TIME_LIMIT = 600  # 10 minutes
    CELERYD_TASK_SOFT_TIME_LIMIT = 540  # 9 minutes

    CELERY_ENABLE_REMOTE_CONTROL = True

    CELERY_ENABLE_UTC = True
    CELERY_ALWAYS_EAGER = False

    # Since our workers are embedded in the Odoo process, we can't turn off
    # the server without shutting the workers down.  So it's probably best to
    # keep the tasks in the broker until a worker has finished with them.
    #
    #                                .
    #                               / \
    #                              / ! \
    #                             -------
    #
    # WARNING! You may do otherwise, but then you have to consider shutting
    # down the HTTP downstream server first, wait for all jobs to finish and
    # then shutdown then server.
    CELERY_ACKS_LATE = True

    _CELERYD_PREFETCH_MULTIPLIER = config.get('celery.prefetch_multiplier', 0)
    if not _CELERYD_PREFETCH_MULTIPLIER:
        # Avoid infinite prefetching
        pass
    else:
        CELERYD_PREFETCH_MULTIPLIER = int(_CELERYD_PREFETCH_MULTIPLIER)
    del _CELERYD_PREFETCH_MULTIPLIER

app = _CeleryApp(__name__)
app.config_from_object(Configuration)

# A context for jobs.  All jobs will be executed in this context.
CELERY_JOB = object()


from psycopg2 import OperationalError, errorcodes
PG_CONCURRENCY_ERRORS_TO_RETRY = (
    errorcodes.LOCK_NOT_AVAILABLE,
    errorcodes.SERIALIZATION_FAILURE,
    errorcodes.DEADLOCK_DETECTED
)


# Since a model method may be altered in several addons, we funnel all calls
# to execute a method in a single Celery task.
@app.task(bind=True, max_retries=5)
def task(self, model, methodname, dbname, uid, args, kwargs):
    with Environment.manage():
        RegistryManager.check_registry_signaling(dbname)
        registry = RegistryManager.get(dbname)
        with registry.cursor() as cr:
            method = getattr(registry[model], methodname)
            if method:
                # It's up to the user to return transferable things.
                try:
                    options = dict(job=self, registry=registry, cr=cr, uid=uid)
                    with _exec_context(CELERY_JOB, **options):
                        res = method(cr, uid, *args, **kwargs)
                    if self.request.id:
                        _report_success.delay(dbname, uid, self.request.id, result=res)
                except celery.exceptions.SoftTimeLimitExceeded as error:
                    cr.rollback()
                    if self.request.id:
                        _report_current_failure(dbname, uid, self.request.id,
                                                error)
                    raise
                except OperationalError as error:
                    cr.rollback()
                    if error.pgcode not in PG_CONCURRENCY_ERRORS_TO_RETRY:
                        _report_current_failure(dbname, uid, self.request.id,
                                                error)
                        raise
                    else:
                        self.retry(args=(model, methodname, dbname, uid,
                                         args, kwargs))
                except Exception as error:
                    _report_current_failure(dbname, uid, self.request.id,
                                            error)
                    raise
            else:
                raise TypeError(
                    'Invalid method name %r for model %r' % (methodname, model)
                )


@app.task(bind=True, max_retries=5)
def _report_success(self, dbname, uid, job_uuid, result=None):
    try:
        with Environment.manage():
            registry = RegistryManager.get(dbname)
            with registry.cursor() as cr:
                _send(get_progress_channel(job_uuid),
                      dict(status='success', result=result),
                      registry=registry, cr=cr, uid=uid)
    except Exception as error:
        self.retry(args=(dbname, uid, job_uuid))


@app.task(bind=True, max_retries=5)
def _report_failure(self, dbname, uid, job_uuid, tb, message=''):
    try:
        with Environment.manage():
            registry = RegistryManager.get(dbname)
            with registry.cursor() as cr:
                _send(get_progress_channel(job_uuid),
                      dict(status='failure', traceback=tb, message=message),
                      registry=registry, cr=cr, uid=uid)
    except Exception as error:
        self.retry(args=(dbname, uid, job_uuid, tb),
                   kwargs={'message': message})


def _report_current_failure(dbname, uid, job_uuid, error):
    import traceback
    message = getattr(error, 'message', '')
    _report_failure.delay(dbname, uid, job_uuid, traceback.format_exc(),
                          message=message)
    logger.exception('Unhandled exception in task')


def _getargs(model, method, cr, uid, *args, **kwargs):
    from openerp.models import Model
    from openerp.sql_db import Cursor
    from openerp.tools import frozendict
    if isinstance(model, Model):
        model = model._name
    if isinstance(cr, Cursor):
        dbname = cr.dbname
    else:
        dbname = cr
    odoo_context = kwargs.get('context', None)
    if isinstance(odoo_context, frozendict):
        kwargs['context'] = dict(odoo_context)
    return (model, method, dbname, uid, args, kwargs)


def get_progress_channel(job):
    '''Get the name of the Odoo bus channel for reporting progress.

    :param job: Either the UUID or the job (a bound Task) instance that must
                have a 'request' attribute.

    '''
    uuid = extract_attrs(job, 'request.id', default=job)
    return 'celeryapp:%s:progress' % uuid


def get_status_channel(job):
    '''Get the name of the Odoo bus channel for reporting status.

    :param job: Either the UUID or the job (a bound Task) instance that must
                have a 'request' attribute.

    '''
    uuid = extract_attrs(job, 'request.id', default=job)
    return 'celeryapp:%s:status' % uuid


def _send(channel, message, registry=None, cr=None, uid=None):
    if registry is None or cr is None or uid is None:
        _context = _exec_context[CELERY_JOB]
        registry = _context['registry']
        cr = _context['cr']
        uid = _context['uid']
    registry['bus.bus'].sendone(cr, uid, channel, message)
