#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# ---------------------------------------------------------------------
# celeryapp
# ---------------------------------------------------------------------
# Copyright (c) 2015-2017 Merchise Autrement [~º/~] and Contributors
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

import contextlib
import threading

import logging
logger = logging.getLogger(__name__)
del logging

from xoutil.context import context as ExecutionContext
from xoutil.objects import extract_attrs

from kombu import Exchange, Queue

from celery import Celery as _CeleryApp

import openerp.tools.config as config
from openerp.tools.func import lazy_property

from openerp.release import version_info
from openerp.api import Environment
from openerp.modules.registry import RegistryManager
from openerp.http import serialize_exception as _serialize_exception

from psycopg2 import OperationalError, errorcodes


# The queues are named using the version info.  This is to avoid clashes with
# other systems using Celery and the same broker.  I've seen sentry tasks
# being routed to one of workers.

# TODO: Write an auto-migration of task routed to older queue names.
ROUTE_NS = 'odoo-{}'.format('.'.join(str(x) for x in version_info[:2]))


def queue(name):
    '''Return the fully qualified queue `name`.

    All queue names must be obtained from this function.  Passing a 'bare'
    queue name in any of the methods can be done, but it's not adviced.

    This function is idempotent::

    >>> queue(queue('x')) == queue('x')
    True

    '''
    if not name.startswith(ROUTE_NS + '.'):
        return '{}.{}'.format(ROUTE_NS, name)
    else:
        return name


# The default pre-created queue.  Although we will allows several queues, we
# strongly advice against creating any-more than the ones defined below.  If
# unsure, just use the default queue.
#
# WARNING: You must run the workers for the non-default queues yourself.
DEFAULT_QUEUE_NAME = queue('default')
del version_info


class DeferredType(object):
    __name__ = 'DeferredType'  # needed by deprecation below

    def __init__(self, **options):
        '''Create a function for a deferred job in the default queue.

        :keyword allow_nested: If True, jobs created with the returning function
                               will be allowed to run nested (within the contex/t
                               of another background job).

                               The default is False.

        :keyword queue: The name of the queue.

        '''
        self.__disallow_nested = not options.pop('allow_nested', False)
        options.setdefault('queue', DEFAULT_QUEUE_NAME)
        self.__options = options

    @property
    def disallow_nested(self):
        return self.__disallow_nested

    @property
    def options(self):
        return dict(self.__options)

    def __call__(self, *args, **kwargs):
        '''Request to run a method in a celery worker.

        The job will be routed to the '{queue}' priority queue.  The signature
        is like::

           Deferred(self.search, [], limit=1)

        The first argument must be a *bound method of a record set*.  The rest
        of the arguments must match the signature of such method.

        :returns: An AsyncResult that represents the job.

        .. warning:: Nested calls don't issue sub-tasks.

           When running inside a background job, calling this function
           **won't** create another background job, but inline the function
           call.

        .. seealso: `DefaultDeferredType`:func:

        '''
        signature = _extract_signature(args, kwargs)
        if self.disallow_nested and CELERY_JOB in ExecutionContext:
            logger.warn('Nested background call detected for model',
                        extra=dict(
                            args_=signature,
                        ))
            return task(*signature)
        else:
            return task.apply_async(args=signature, **self.options)


Deferred = DeferredType()


from xoutil.deprecation import deprecated   # noqa
DefaultDeferredType = deprecated(DeferredType)(DeferredType)
LowPriorityDeferredType = HighPriorityDeferredType = deprecated(
    DeferredType,
    'LowPriorityDeferredType and HighPriorityDeferredType '
    'are deprecated, use DeferredType'
)(DeferredType)
LowPriorityDeferrred = HighPriorityDeferred = deprecated(
    Deferred,
    'LowPriorityDeferred and HighPriorityDeferred '
    'are deprecated, use Deferred'
)(Deferred)
del deprecated


def iter_and_report(iterator, valuemax=None, report_rate=1,
                    messagetmpl='Progress: {progress}'):
    '''Iterate over 'iterator' while reporting progress.

    In the context of a background (celery) job you may wish to iterate over a
    sequence of objects and, at the same time, report progress.

    Return a generator that consumes and produces the same values as
    `iterator`.

    Report progress is disabled if `valuemax` is not a positive integral
    value.

    `report_rate` regulates the rate at which reports are issued: Reports are
    issued only when then progress is an integral multiple of `rate` (or when
    it's zero).

    When the `iterator` is fully consumed, despite the value of `report_rate`,
    we issue a final report making progress=valuemax (i.e. 100%).

    The `messagetmpl` is a string template to format the message to be
    reported.  The allowed keywords in the template are 'progress' and
    'valuemax' (the provided argument).

    .. rubric:: Co-routine behavior

    At each step if you send back a value, it should be a string with a new
    message template.

    '''
    from xoutil.eight import integer_types, string_types
    if not all(isinstance(x, integer_types) for x in (valuemax, report_rate)):
        raise TypeError('valuemax and step most be integers')
    if not isinstance(messagetmpl, string_types):
        raise TypeError('messagetmpl must a string')
    for progress, x in enumerate(iterator):
        if valuemax and progress % report_rate == 0:
            report_progress(
                message=messagetmpl.format(progress=progress,
                                           valuemax=valuemax),
                progress=progress, valuemax=valuemax, valuemin=0
            )
        msg = yield x
        if msg and isinstance(msg, string_types):
            messagetmpl = msg
    if valuemax:
        report_progress(progress=valuemax)  # 100%


def until_timeout(iterator):
    '''Iterate and yield from `iter` while the job has time to work.

    Celery can be configured to raise a SoftTimeLimitExceeded exception when a
    soft time limit is reached.

    This function integrates such signal into a background job that does its
    work by yielding each *partially complete* unit of progress.

    It's expected that it will be more likely for StopTimeLimitExceeded to be
    raised while `iterator` it's performing work, than the the consumers.  In
    other word, you should enclose as much work as possible within a single
    call to `until_timeout`.

    .. note:: This requires xoutil 1.7.2.  If that version of xoutil is not
       installed `until_timeout` simply returns `iterator` unchanged.

    '''
    from celery.exceptions import SoftTimeLimitExceeded
    try:
        from xoutil.bound import until

        @until(errors=(SoftTimeLimitExceeded, ))
        def _iterate():
            for x in iterator:
                yield x

        # We need to expose the generator, not the last value.  Yes, it's
        # possible StopTimeLimitExceeded to be raised outside this generator,
        # but you have been warned to put this as farther as possible from the
        # true computation.
        return _iterate.generate()
    except ImportError:
        return iterator


def report_progress(message=None, progress=None, valuemin=None, valuemax=None,
                    status=None):
    '''Send a progress notification to whomever is polling the current job.

    :param message: The message to send to those waiting for the message.

    :param progress: A number in the range given by `valuemin` and `valuemax`
           indicating how much has been done.

           If you can't produce a good estimate is best to send "stages" in
           the message.

    :param valuemin: The minimum value `progress` can take.

    :param valuemax: The maximum value `progress` can take.

    The `valuemin` and `valuemax` arguments must be reported together.  And
    once settle they cannot be changed.

    :param status: The reported status. This should be one of the strings
       'success', 'failure' or 'pending'.

       .. warning:: This argument should not be used but for internal (job
                    framework module) purposes.

    '''
    _context = ExecutionContext[CELERY_JOB]
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
    broker_url = BROKER_URL = config.get('celery.broker', 'redis://localhost/9')
    # We don't use the backend to store results, but send results via another
    # message.  However to check the job status the backend is used.
    result_backend = CELERY_RESULT_BACKEND = config.get('celery.backend', None)

    task_default_queue = CELERY_DEFAULT_QUEUE = DEFAULT_QUEUE_NAME
    task_default_exchange_type = CELERY_DEFAULT_EXCHANGE_TYPE = 'direct'
    task_default_routing_key = CELERY_DEFAULT_ROUTING_KEY = DEFAULT_QUEUE_NAME

    worker_send_task_events = CELERYD_SEND_EVENTS = True

    # Maximum number of tasks a pool worker process can execute before it’s
    # replaced with a new one. Default is no limit.
    worker_max_tasks_per_child = CELERYD_MAX_TASKS_PER_CHILD = 2000

    # Maximum amount of resident memory, in kilobytes, that may be consumed by
    # a worker before it will be replaced by a new worker. If a single task
    # causes a worker to exceed this limit, the task will be completed, and
    # the worker will be replaced afterwards.
    _worker_max_memory_per_child = config.get('celery.worker_max_memory_per_child')
    if _worker_max_memory_per_child:
        worker_max_memory_per_child = _worker_max_memory_per_child
    del _worker_max_memory_per_child

    # TODO: Take queues from configuration.
    task_queues = CELERY_QUEUES = (
        Queue(DEFAULT_QUEUE_NAME, Exchange(DEFAULT_QUEUE_NAME),
              routing_key=DEFAULT_QUEUE_NAME),
    )
    task_create_missing_queues = CELERY_CREATE_MISSING_QUEUES = config.get(
        'celery.create_missing_queues',
        True
    )

    task_time_limit = CELERYD_TASK_TIME_LIMIT = config.get('celery.task_time_limit', 600)  # 10 minutes
    _softtime = config.get('celery.task_soft_time_limit', None)
    if _softtime is not None:
        task_soft_time_limit = CELERYD_TASK_SOFT_TIME_LIMIT = int(_softtime)
    del _softtime

    worker_enable_remote_control = CELERY_ENABLE_REMOTE_CONTROL = True

    enable_utc = CELERY_ENABLE_UTC = True
    task_always_eager = CELERY_ALWAYS_EAGER = False
    task_acks_late = CELERY_ACKS_LATE = config.get('celery.acks_late', True)

    _CELERYD_PREFETCH_MULTIPLIER = config.get('celery.prefetch_multiplier', 0)
    if not _CELERYD_PREFETCH_MULTIPLIER:
        # Avoid infinite prefetching
        pass
    else:
        worker_prefetch_multiplier = CELERYD_PREFETCH_MULTIPLIER = int(_CELERYD_PREFETCH_MULTIPLIER)  # noqa
    del _CELERYD_PREFETCH_MULTIPLIER

    _CELERYBEAT_SCHEDULE_FILENAME = config.get(
        'celery.beat_schedule_filename',
        None
    )
    if _CELERYBEAT_SCHEDULE_FILENAME is not None:
        beat_schedule_filename = CELERYBEAT_SCHEDULE_FILENAME = _CELERYBEAT_SCHEDULE_FILENAME  # noqa
    del _CELERYBEAT_SCHEDULE_FILENAME


app = _CeleryApp(__name__)
app.config_from_object(Configuration)


# A context for jobs.  All jobs will be executed in this context.
class CELERY_JOB(ExecutionContext):
    def __new__(cls, **options):
        context_identifier = cls
        return super(CELERY_JOB, cls).__new__(
            cls, context_identifier, **options
        )

    def __init__(self, **options):
        super(CELERY_JOB, self).__init__(**options)
        self.job = options['job']
        self.env = options['env']

    @lazy_property
    def request(self):
        class req(object):
            # A request-like object.
            #
            # ``bool(req())`` is always False.
            #
            # ``req().anything`` is another ``req()``, so you can do
            # ``req().x.y.z``.  This fact, combined with the previous, means
            # that ``bool(req().anything.not.shown.below)`` is always False.
            #
            # This is a technical hack to make parts of Odoo that require a
            # HTTP request in the `openerp.http.request`:object: to be
            # available, and many attributes are also freely traversed like
            # ``request.httprequest.is_spdy``...
            #
            env = self.env
            uid = env.uid
            context = env.context
            lang = context.get('lang', 'en_US')
            cr = env.cr
            _cr = env.cr
            db = env.cr.dbname

            def __nonzero__(self):
                return False
            __bool__ = __nonzero__

            def __getattr__(self, attr):
                return req()

            @contextlib.contextmanager
            def registry_cr(self):
                import warnings
                warnings.warn(
                    'please use request.registry and request.cr directly',
                    DeprecationWarning
                )
                yield (self.registry, self.cr)

        return req()

    def __enter__(self):
        from openerp.http import _request_stack
        _request_stack.push(self.request)
        return super(CELERY_JOB, self).__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        from openerp.http import _request_stack
        _request_stack.pop()
        return super(CELERY_JOB, self).__exit__(exc_type, exc_val, exc_tb)


PG_CONCURRENCY_ERRORS_TO_RETRY = (
    errorcodes.LOCK_NOT_AVAILABLE,
    errorcodes.SERIALIZATION_FAILURE,
    errorcodes.DEADLOCK_DETECTED
)


def _extract_signature(args, kwargs):
    '''Detect the proper signature.

    '''
    from xoutil import Unset
    from openerp.models import BaseModel
    from openerp.sql_db import Cursor
    from openerp.tools import frozendict
    method = args[0]
    self = getattr(method, 'im_self', Unset)
    env = getattr(self, 'env', Unset)
    if isinstance(self, BaseModel) and isinstance(env, Environment):
        db, uid, context = env.args
        kwargs['context'] = dict(context)
        model = self
        methodname = method.__name__
        ids = self.ids
        args = args[1:]
    else:
        model, db, uid, methodname = args[:4]
        args = args[4:]
        ids = None
    if isinstance(model, BaseModel):
        model = model._name
    elif isinstance(model, type(BaseModel)):
        model = getattr(model, '_name', None) or model._inherit
    if isinstance(db, Cursor):
        dbname = db.dbname
    else:
        dbname = db
    odoo_context = kwargs.get('context', None)
    if isinstance(odoo_context, frozendict):
        kwargs['context'] = dict(odoo_context)
    return model, ids, methodname, dbname, uid, args, kwargs


# Since a model method may be altered in several addons, we funnel all calls
# to execute a method in a single Celery task.
@app.task(bind=True, max_retries=5)
def task(self, model, ids, methodname, dbname, uid, args, kwargs):
    from openerp.models import BaseModel
    context = kwargs.pop('context', None)
    with MaybeRecords(dbname, uid, model, ids, context=context) as r:
        method = getattr(r, methodname)
        if method:
            if not ids and _require_ids(method):
                ids = args[0]
                args = args[1:]
                method = getattr(r.browse(ids), methodname)
            # It's up to the user to return transferable things.
            try:
                options = dict(job=self, env=r.env)
                with CELERY_JOB(**options):
                    res = method(*args, **kwargs)
                if isinstance(res, BaseModel):
                    res = res.ids  # downgrade to ids
                if self.request.id:
                    _report_success.delay(dbname, uid, self.request.id,
                                          result=res)
            except OperationalError as error:
                if error.pgcode not in PG_CONCURRENCY_ERRORS_TO_RETRY:
                    if self.request.id:
                        _report_current_failure(dbname, uid, self.request.id,
                                                error)
                    raise
                else:
                    # This method raises celery.exceptions.Retry
                    self.retry(args=(model, methodname, dbname, uid,
                                     args, kwargs))
            except Exception as error:
                if self.request.id:
                    _report_current_failure(dbname, uid, self.request.id,
                                            error)
                raise
        else:
            raise TypeError(
                'Invalid method name %r for model %r' % (methodname, model)
            )


@contextlib.contextmanager
def MaybeRecords(dbname, uid, model, ids=None, cr=None, context=None):
    __traceback_hide__ = True  # noqa: hide from Celery Tracebacks
    with OdooEnvironment(dbname, uid, cr=cr, context=context) as env:
        records = env[model].browse(ids)
        yield records


@contextlib.contextmanager
def OdooEnvironment(dbname, uid, cr=None, context=None):
    __traceback_hide__ = True  # noqa: hide from Celery Tracebacks
    with Environment.manage():
        RegistryManager.check_registry_signaling(dbname)
        registry = RegistryManager.get(dbname)
        # Several pieces of OpenERP code expect this attributes to be set in the
        # current thread.
        threading.current_thread().uid = uid
        threading.current_thread().dbname = dbname
        try:
            if cr is None:
                cr = registry.cursor()
                closing = lambda c: c  # noqa
            else:
                closing = noop
            with closing(cr) as cr2:
                env = Environment(cr2, uid, context or {})
                yield env
        finally:
            if hasattr(threading.current_thread(), 'uid'):
                del threading.current_thread().uid
            if hasattr(threading.current_thread(), 'dbname'):
                del threading.current_thread().dbname


@contextlib.contextmanager
def noop(c):
    yield c


def _require_ids(method):
    from openerp.api import (multi, cr_uid_id, cr_uid_id_context, cr_uid_ids,
                             cr_uid_ids_context)
    return getattr(method, '_api', None) in (multi, cr_uid_id,
                                             cr_uid_id_context,
                                             cr_uid_ids,
                                             cr_uid_ids_context)
    return False


@app.task(bind=True, max_retries=5)
def _report_success(self, dbname, uid, job_uuid, result=None):
    try:
        with OdooEnvironment(dbname, uid) as env:
            _send(
                get_progress_channel(job_uuid),
                dict(status='success', result=result),
                env=env
            )
    except Exception:
        self.retry(args=(dbname, uid, job_uuid))


@app.task(bind=True, max_retries=5)
def _report_failure(self, dbname, uid, job_uuid, tb=None, message=''):
    try:
        with OdooEnvironment(dbname, uid) as env:
            _send(
                get_progress_channel(job_uuid),
                dict(status='failure', traceback=tb, message=message),
                env=env

            )
    except Exception:
        self.retry(args=(dbname, uid, job_uuid, tb),
                   kwargs={'message': message})


def _report_current_failure(dbname, uid, job_uuid, error):
    data = _serialize_exception(error)
    _report_failure.delay(dbname, uid, job_uuid, message=data)
    logger.exception('Unhandled exception in task')


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


def _send(channel, message, env=None):
    if env is None:
        _context = ExecutionContext[CELERY_JOB]
        env = _context['env']
    env['bus.bus'].sendone(channel, message)
