# -*- coding: utf-8 -*-
#
# This file is part of SENAITE.CORE
#
# Copyright 2018 by it's authors.
# Some rights reserved. See LICENSE.rst, CONTRIBUTORS.rst.

import collections
import sys

from AccessControl.SecurityInfo import ModuleSecurityInfo
from Products.CMFCore.WorkflowCore import WorkflowException
from Products.CMFCore.utils import getToolByName
from bika.lims import PMF
from bika.lims import enum, api
from bika.lims import logger
from bika.lims.browser import ulocalized_time
from bika.lims.interfaces import IJSONReadExtender
from bika.lims.jsonapi import get_include_fields
from bika.lims.utils import changeWorkflowState
from bika.lims.utils import t
from bika.lims.workflow.indexes import ACTIONS_TO_INDEXES
from zope.interface import implements

security = ModuleSecurityInfo('bika.lims.workflow')
security.declarePublic('guard_handler')

_marker = object()

def skip(instance, action, peek=False, unskip=False):
    """Returns True if the transition is to be SKIPPED

        peek - True just checks the value, does not set.
        unskip - remove skip key (for manual overrides).

    called with only (instance, action_id), this will set the request variable preventing the
    cascade's from re-transitioning the object and return None.
    """

    uid = callable(instance.UID) and instance.UID() or instance.UID
    skipkey = "%s_%s" % (uid, action)
    if 'workflow_skiplist' not in instance.REQUEST:
        if not peek and not unskip:
            instance.REQUEST['workflow_skiplist'] = [skipkey, ]
    else:
        if skipkey in instance.REQUEST['workflow_skiplist']:
            if unskip:
                instance.REQUEST['workflow_skiplist'].remove(skipkey)
            else:
                return True
        else:
            if not peek and not unskip:
                instance.REQUEST["workflow_skiplist"].append(skipkey)


def doActionFor(instance, action_id, idxs=None):
    """Tries to perform the transition to the instance.
    Object is reindexed after the transition takes place, but only if succeeds.
    If idxs is set, only these indexes will be reindexed. Otherwise, will try
    to use the indexes defined in ACTIONS_TO_INDEX mapping if any.
    :param instance: Object to be transitioned
    :param action_id: transition id
    :param idxs: indexes to be reindexed after the transition
    :returns: True if the transition has been performed, together with message
    :rtype: tuple (bool,str)
    """
    if not instance:
        return False, ""

    if isinstance(instance, list):
        # TODO Workflow . Check if this is strictly necessary
        # This check is here because sometimes Plone creates a list
        # from submitted form elements.
        logger.warn("Got a list of obj in doActionFor!")
        if len(instance) > 1:
            logger.warn(
                "doActionFor is getting an instance parameter which is a list "
                "with more than one item. Instance: '{}', action_id: '{}'"
                .format(instance, action_id)
            )

        return doActionFor(instance=instance[0], action_id=action_id, idxs=idxs)

    # Since a given transition can cascade or promote to other objects, we want
    # to reindex all objects for which the transition succeed at once, at the
    # end of process. Otherwise, same object will be reindexed multiple times
    # unnecessarily. Also, ActionsHandlerPool ensures the same transition is not
    # applied twice to the same object due to cascade/promote recursions.
    pool = ActionHandlerPool.get_instance()
    if pool.succeed(instance, action_id):
        return False, "Transition {} for {} already done"\
             .format(action_id, instance.getId())

    # Return False if transition is not permitted
    if not isTransitionAllowed(instance, action_id):
        return False, "Transition {} for {} is not allowed"\
            .format(action_id, instance.getId())

    # Add this batch process to the queue
    pool.queue_pool()
    succeed = False
    message = ""
    workflow = getToolByName(instance, "portal_workflow")
    try:
        workflow.doActionFor(instance, action_id)
        succeed = True
    except WorkflowException as e:
        message = str(e)
        curr_state = getCurrentState(instance)
        clazz_name = instance.__class__.__name__
        logger.warning(
            "Transition '{0}' not allowed: {1} '{2}' ({3})"\
            .format(action_id, clazz_name, instance.getId(), curr_state))
        logger.error(message)

    # If no indexes to reindex have been defined, try to use those defined in
    # the ACTIONS_TO_INDEXES mapping. Reindexing only those indexes that might
    # be affected by the transition boosts the overall performance!.
    if idxs is None:
        portal_type = instance.portal_type
        idxs = ACTIONS_TO_INDEXES.get(portal_type, {}).get(action_id, [])

    # Add the current object to the pool and resume
    pool.push(instance, action_id, succeed, idxs=idxs)
    pool.resume()

    return succeed, message


# TODO Workflow - remove doAction(s)For?
def doActionsFor(instance, actions):
    """Performs a set of transitions to the instance passed in
    """
    pool = ActionHandlerPool.get_instance()
    pool.queue_pool()
    for action in actions:
        doActionFor(instance, action)
    pool.resume()


def call_workflow_event(instance, event, after=True):
    """Calls the instance's workflow event
    """
    if not event.transition:
        return False

    portal_type = instance.portal_type
    wf_module = _load_wf_module('{}.events'.format(portal_type.lower()))
    if not wf_module:
        return False

    # Inspect if event_<transition_id> function exists in the module
    prefix = after and "after" or "before"
    func_name = "{}_{}".format(prefix, event.transition.id)
    func = getattr(wf_module, func_name, False)
    if not func:
        return False

    logger.info('WF event: {0}.events.{1}'
                .format(portal_type.lower(), func_name))
    func(instance)
    return True


def BeforeTransitionEventHandler(instance, event):
    """ This event is executed before each transition and delegates further
    actions to 'workflow.<portal_type>.events.before_<transition_id> function
    if exists for the instance passed in.
    :param instance: the instance to be transitioned
    :type instance: ATContentType
    :param event: event that holds the transition to be performed
    :type event: IObjectEvent
    """
    call_workflow_event(instance, event, after=False)


def AfterTransitionEventHandler(instance, event):
    """ This event is executed after each transition and delegates further
    actions to 'workflow.<portal_type>.events.after_<transition_id> function
    if exists for the instance passed in.
    :param instance: the instance that has been transitioned
    :type instance: ATContentType
    :param event: event that holds the transition performed
    :type event: IObjectEvent
    """
    if call_workflow_event(instance, event, after=True):
        return

    # Try with old AfterTransitionHandler dance...
    # TODO CODE TO BE REMOVED AFTER PORTING workflow_script_*/*_transition_event
    if not event.transition:
        return
    # Set the request variable preventing cascade's from re-transitioning.
    if skip(instance, event.transition.id):
        return
    # Because at this point, the object has been transitioned already, but
    # further actions are probably needed still, so be sure is reindexed
    # before going forward.
    instance.reindexObject()
    key = 'after_{0}_transition_event'.format(event.transition.id)
    after_event = getattr(instance, key, False)
    if not after_event:
        # TODO Workflow. this conditional is only for backwards compatibility,
        # to be removed when all workflow_script_* methods in contents are
        # replaced by the more explicity signature 'after_*_transition_event'
        key = 'workflow_script_' + event.transition.id
        after_event = getattr(instance, key, False)
    if not after_event:
        return
    after_event()


def get_workflow_actions(obj):
    """ Compile a list of possible workflow transitions for this object
    """

    def translate(id):
        return t(PMF(id + "_transition_title"))

    transids = getAllowedTransitions(obj)
    actions = [{'id': it, 'title': translate(it)} for it in transids]
    return actions


def isBasicTransitionAllowed(context, permission=None):
    """Most transition guards need to check the same conditions:

    - Is the object active (cancelled or inactive objects can't transition)
    - Has the user a certain permission, required for transition.  This should
    normally be set in the guard_permission in workflow definition.

    """
    workflow = getToolByName(context, "portal_workflow")
    mtool = getToolByName(context, "portal_membership")
    if not isActive(context) \
        or (permission and mtool.checkPermission(permission, context)):
        return False
    return True


def isTransitionAllowed(instance, transition_id):
    """Checks if the object can perform the transition passed in.
    :returns: True if transition can be performed
    :rtype: bool
    """
    if transition_id not in ['reinstate', 'activate']:
        if not api.is_active(instance):
            return False

    wf_tool = getToolByName(instance, "portal_workflow")
    for wf_id in wf_tool.getChainFor(instance):
        wf = wf_tool.getWorkflowById(wf_id)
        if wf and wf.isActionSupported(instance, transition_id):
            return True

    return False


def getAllowedTransitions(instance):
    """Returns a list with the transition ids that can be performed against
    the instance passed in.
    :param instance: A content object
    :type instance: ATContentType
    :returns: A list of transition/action ids
    :rtype: list
    """
    wftool = getToolByName(instance, "portal_workflow")
    transitions = wftool.getTransitionsFor(instance)
    return [trans['id'] for trans in transitions]


def wasTransitionPerformed(instance, transition_id):
    """Checks if the transition has already been performed to the object
    Instance's workflow history is checked.
    """
    transitions = getReviewHistoryActionsList(instance)
    return transition_id in transitions


def isActive(instance):
    """Returns True if the object is neither in a cancelled nor inactive state
    """
    return api.is_active(instance)


def getReviewHistoryActionsList(instance):
    """Returns a list with the actions performed for the instance, from oldest
    to newest"""
    review_history = getReviewHistory(instance)
    review_history.reverse()
    actions = [event['action'] for event in review_history]
    return actions


def getReviewHistory(instance):
    """Returns the review history for the instance in reverse order
    :returns: the list of historic events as dicts
    """
    review_history = []
    workflow = getToolByName(instance, 'portal_workflow')
    try:
        review_history = list(workflow.getInfoFor(instance, 'review_history'))
    except WorkflowException:
        logger.error("Unable to retrieve review history from {}:{}"
                     .format(instance.portal_type, instance.getId()))
    # invert the list, so we always see the most recent matching event
    review_history.reverse()
    return review_history

def getCurrentState(obj, stateflowid='review_state'):
    """ The current state of the object for the state flow id specified
        Return empty if there's no workflow state for the object and flow id
    """
    return api.get_workflow_status_of(obj, stateflowid)

def in_state(obj, states, stateflowid='review_state'):
    """ Returns if the object passed matches with the states passed in
    """
    if not states:
        return False
    obj_state = getCurrentState(obj, stateflowid=stateflowid)
    return obj_state in states

def getTransitionActor(obj, action_id):
    """Returns the actor that performed a given transition. If transition has
    not been perormed, or current user has no privileges, returns None
    :return: the username of the user that performed the transition passed-in
    :type: string
    """
    review_history = getReviewHistory(obj)
    for event in review_history:
        if event.get('action') == action_id:
            return event.get('actor')
    return None


def getTransitionDate(obj, action_id, return_as_datetime=False):
    """
    Returns date of action for object. Sometimes we need this date in Datetime
    format and that's why added return_as_datetime param.
    """
    review_history = getReviewHistory(obj)
    for event in review_history:
        if event.get('action') == action_id:
            evtime = event.get('time')
            if return_as_datetime:
                return evtime
            if evtime:
                value = ulocalized_time(evtime, long_format=True,
                                        time_only=False, context=obj)
                return value
    return None


def getTransitionUsers(obj, action_id, last_user=False):
    """
    This function returns a list with the users who have done the transition.
    :action_id: a sring as the transition id.
    :last_user: a boolean to return only the last user triggering the
        transition or all of them.
    :returns: a list of user ids.
    """
    workflow = getToolByName(obj, 'portal_workflow')
    users = []
    try:
        # https://jira.bikalabs.com/browse/LIMS-2242:
        # Sometimes the workflow history is inexplicably missing!
        review_history = list(workflow.getInfoFor(obj, 'review_history'))
    except WorkflowException:
        logger.error(
            "workflow history is inexplicably missing."
            " https://jira.bikalabs.com/browse/LIMS-2242")
        return users
    # invert the list, so we always see the most recent matching event
    review_history.reverse()
    for event in review_history:
        if event.get('action', '') == action_id:
            value = event.get('actor', '')
            users.append(value)
            if last_user:
                return users
    return users


def guard_handler(instance, transition_id):
    """Generic workflow guard handler that returns true if the transition_id
    passed in can be performed to the instance passed in.

    This function is called automatically by a Script (Python) located at
    bika/lims/skins/guard_handler.py, which in turn is fired by Zope when an
    expression like "python:here.guard_handler('<transition_id>')" is set to
    any given guard (used by default in all bika's DC Workflow guards).

    Walks through bika.lims.workflow.<obj_type>.guards and looks for a function
    that matches with 'guard_<transition_id>'. If found, calls the function and
    returns its value (true or false). If not found, returns True by default.

    :param instance: the object for which the transition_id has to be evaluated
    :param transition_id: the id of the transition
    :type instance: ATContentType
    :type transition_id: string
    :return: true if the transition can be performed to the passed in instance
    :rtype: bool
    """
    if not instance:
        return True
    clazz_name = instance.portal_type
    # Inspect if bika.lims.workflow.<clazzname>.<guards> module exists
    wf_module = _load_wf_module('{0}.guards'.format(clazz_name.lower()))
    if not wf_module:
        return True

    # Inspect if guard_<transition_id> function exists in the above module
    key = 'guard_{0}'.format(transition_id)
    guard = getattr(wf_module, key, False)
    if not guard:
        return True

    #logger.info('{0}.guards.{1}'.format(clazz_name.lower(), key))
    return guard(instance)


def _load_wf_module(module_relative_name):
    """Loads a python module based on the module relative name passed in.

    At first, tries to get the module from sys.modules. If not found there, the
    function tries to load it by using importlib. Returns None if no module
    found or importlib is unable to load it because of errors.
    Eg:
        _load_wf_module('sample.events')

    will try to load the module 'bika.lims.workflow.sample.events'

    :param modrelname: relative name of the module to be loaded
    :type modrelname: string
    :return: the module
    :rtype: module
    """
    if not module_relative_name:
        return None
    if not isinstance(module_relative_name, basestring):
        return None

    rootmodname = __name__
    modulekey = '{0}.{1}'.format(rootmodname, module_relative_name)
    if modulekey in sys.modules:
        return sys.modules.get(modulekey, None)

    # Try to load the module recursively
    modname = None
    tokens = module_relative_name.split('.')
    for part in tokens:
        modname = '.'.join([modname, part]) if modname else part
        import importlib
        try:
            _module = importlib.import_module('.'+modname, package=rootmodname)
            if not _module:
                return None
        except Exception:
            return None
    return sys.modules.get(modulekey, None)


# Enumeration of the available status flows
StateFlow = enum(review='review_state',
                 inactive='inactive_state',
                 cancellation='cancellation_state')

# Enumeration of the different available states from the inactive flow
InactiveState = enum(active='active')

# Enumeration of the different states can have a batch
BatchState = enum(open='open',
                  closed='closed',
                  cancelled='cancelled')

BatchTransitions = enum(open='open',
                        close='close')

CancellationState = enum(active='active',
                         cancelled='cancelled')

CancellationTransitions = enum(cancel='cancel',
                               reinstate='reinstate')


class JSONReadExtender(object):

    """- Adds the list of possible transitions to each object, if 'transitions'
    is specified in the include_fields.
    """

    implements(IJSONReadExtender)

    def __init__(self, context):
        self.context = context

    def __call__(self, request, data):
        include_fields = get_include_fields(request)
        if not include_fields or "transitions" in include_fields:
            data['transitions'] = get_workflow_actions(self.context)


# TODO Workflow - ActionsPool - Better use ActionHandlerPool
class ActionsPool(object):
    """Handles transitions of multiple objects at once
    """
    def __init__(self):
        self.actions_pool = collections.OrderedDict()

    def add(self, instance, action_id):
        uid = api.get_uid(instance)
        self.actions_pool[uid] = {"instance": instance,
                                  "action_id": action_id}

    def resume(self):
        action_handler = ActionHandlerPool.get_instance()
        action_handler.queue_pool()
        outcome = collections.OrderedDict()
        for uid, values in self.actions_pool.items():
            instance = values["instance"]
            action_id = values["action_id"]
            outcome[uid] = doActionFor(instance, action_id)
        self.actions_pool = collections.OrderedDict()
        action_handler.resume()
        return outcome


class ActionHandlerPool(object):
    """Singleton to handle concurrent transitions
    """
    __instance = None

    @staticmethod
    def get_instance():
        """Returns the current instance of ActionHandlerPool
        """
        if ActionHandlerPool.__instance == None:
            ActionHandlerPool()
        return ActionHandlerPool.__instance

    def __init__(self):
        if ActionHandlerPool.__instance != None:
            raise Exception("Use ActionHandlerPool.get_instance()")
        self.objects = collections.OrderedDict()
        self.num_calls = 0
        ActionHandlerPool.__instance = self

    def __len__(self):
        """Number of objects in the pool
        """
        return len(self.objects)

    def queue_pool(self):
        """Notifies that a new batch of jobs is about to begin
        """
        self.num_calls += 1

    def push(self, instance, action, success, idxs=_marker):
        """Adds an instance into the pool, to be reindexed on resume
        """
        uid = api.get_uid(instance)
        info = self.objects.get(uid, {})
        idx = [] if idxs is _marker else idxs
        info[action] = {'instance': instance, 'success': success, 'idxs': idx}
        self.objects[uid] = info

    def succeed(self, instance, action):
        """Returns if the task for the instance took place successfully
        """
        uid = api.get_uid(instance)
        return self.objects.get(uid, {}).get(action, {}).get('success', False)

    def resume(self):
        """Resumes the pool and reindex all objects processed
        """
        self.num_calls -= 1
        if self.num_calls > 0:
            return
        logger.info("Resume actions for {} objects".format(len(self)))
        processed = list()
        for uid, info in self.objects.items():
            if uid in processed:
                continue
            instance = info[info.keys()[0]]["instance"]
            idxs = self.get_indexes(uid)
            idxs_str = idxs and ', '.join(idxs) or "-- All indexes --"
            logger.info("Reindexing {}: {}".format(instance.getId(), idxs_str))
            instance.reindexObject(idxs=self.get_indexes(uid))
            processed.append(uid)
        logger.info("Objects processed: {}".format(len(processed)))
        self.objects = collections.OrderedDict()

    def get_indexes(self, uid):
        """Returns the names of the indexes to be reindexed for the object with
        the uid passed in. If no indexes for this object have been specified
        within the action pool job, returns an empty list (reindex all).
        Otherwise, return all the indexes that have been specified for the
        object within the action pool job.
        """
        idxs = []
        info = self.objects.get(uid, {})
        for action_id, value in info.items():
            obj_idxs = value.get('idxs', None)
            if obj_idxs is None:
                # Don't reindex!
                continue
            elif len(obj_idxs) == 0:
                # Reindex all indexes!
                return []
            idxs.extend(obj_idxs)
        # Always reindex review_state
        idxs.append("review_state")
        return list(set(idxs))


def push_reindex_to_actions_pool(obj, idxs=None):
    """Push a reindex job to the actions handler pool
    """
    indexes = idxs and idxs or []
    pool = ActionHandlerPool.get_instance()
    pool.push(obj, "reindex", success=True, idxs=indexes)
