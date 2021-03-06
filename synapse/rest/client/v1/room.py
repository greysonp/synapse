# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" This module contains REST servlets to do with rooms: /rooms/<paths> """
from twisted.internet import defer

from .base import ClientV1RestServlet, client_path_patterns
from synapse.api.errors import SynapseError, Codes, AuthError
from synapse.streams.config import PaginationConfig
from synapse.api.constants import EventTypes, Membership
from synapse.types import UserID, RoomID, RoomAlias
from synapse.events.utils import serialize_event
from synapse.http.servlet import parse_json_object_from_request

import logging
import urllib

logger = logging.getLogger(__name__)


class RoomCreateRestServlet(ClientV1RestServlet):
    # No PATTERN; we have custom dispatch rules here

    def register(self, http_server):
        PATTERNS = "/createRoom"
        register_txn_path(self, PATTERNS, http_server)
        # define CORS for all of /rooms in RoomCreateRestServlet for simplicity
        http_server.register_paths("OPTIONS",
                                   client_path_patterns("/rooms(?:/.*)?$"),
                                   self.on_OPTIONS)
        # define CORS for /createRoom[/txnid]
        http_server.register_paths("OPTIONS",
                                   client_path_patterns("/createRoom(?:/.*)?$"),
                                   self.on_OPTIONS)

    @defer.inlineCallbacks
    def on_PUT(self, request, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)

    @defer.inlineCallbacks
    def on_POST(self, request):
        requester = yield self.auth.get_user_by_req(request)

        handler = self.handlers.room_creation_handler
        info = yield handler.create_room(
            requester, self.get_room_config(request)
        )

        defer.returnValue((200, info))

    def get_room_config(self, request):
        user_supplied_config = parse_json_object_from_request(request)
        return user_supplied_config

    def on_OPTIONS(self, request):
        return (200, {})


# TODO: Needs unit testing for generic events
class RoomStateEventRestServlet(ClientV1RestServlet):
    def register(self, http_server):
        # /room/$roomid/state/$eventtype
        no_state_key = "/rooms/(?P<room_id>[^/]*)/state/(?P<event_type>[^/]*)$"

        # /room/$roomid/state/$eventtype/$statekey
        state_key = ("/rooms/(?P<room_id>[^/]*)/state/"
                     "(?P<event_type>[^/]*)/(?P<state_key>[^/]*)$")

        http_server.register_paths("GET",
                                   client_path_patterns(state_key),
                                   self.on_GET)
        http_server.register_paths("PUT",
                                   client_path_patterns(state_key),
                                   self.on_PUT)
        http_server.register_paths("GET",
                                   client_path_patterns(no_state_key),
                                   self.on_GET_no_state_key)
        http_server.register_paths("PUT",
                                   client_path_patterns(no_state_key),
                                   self.on_PUT_no_state_key)

    def on_GET_no_state_key(self, request, room_id, event_type):
        return self.on_GET(request, room_id, event_type, "")

    def on_PUT_no_state_key(self, request, room_id, event_type):
        return self.on_PUT(request, room_id, event_type, "")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id, event_type, state_key):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)

        msg_handler = self.handlers.message_handler
        data = yield msg_handler.get_room_data(
            user_id=requester.user.to_string(),
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
            is_guest=requester.is_guest,
        )

        if not data:
            raise SynapseError(
                404, "Event not found.", errcode=Codes.NOT_FOUND
            )
        defer.returnValue((200, data.get_dict()["content"]))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, event_type, state_key, txn_id=None):
        requester = yield self.auth.get_user_by_req(request)

        content = parse_json_object_from_request(request)

        event_dict = {
            "type": event_type,
            "content": content,
            "room_id": room_id,
            "sender": requester.user.to_string(),
        }

        if state_key is not None:
            event_dict["state_key"] = state_key

        msg_handler = self.handlers.message_handler
        event, context = yield msg_handler.create_event(
            event_dict,
            token_id=requester.access_token_id,
            txn_id=txn_id,
        )

        if event_type == EventTypes.Member:
            yield self.handlers.room_member_handler.send_membership_event(
                requester,
                event,
                context,
            )
        else:
            yield msg_handler.send_nonmember_event(requester, event, context)

        defer.returnValue((200, {"event_id": event.event_id}))


# TODO: Needs unit testing for generic events + feedback
class RoomSendEventRestServlet(ClientV1RestServlet):

    def register(self, http_server):
        # /rooms/$roomid/send/$event_type[/$txn_id]
        PATTERNS = ("/rooms/(?P<room_id>[^/]*)/send/(?P<event_type>[^/]*)")
        register_txn_path(self, PATTERNS, http_server, with_get=True)

    @defer.inlineCallbacks
    def on_POST(self, request, room_id, event_type, txn_id=None):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)
        content = parse_json_object_from_request(request)

        msg_handler = self.handlers.message_handler
        event = yield msg_handler.create_and_send_nonmember_event(
            requester,
            {
                "type": event_type,
                "content": content,
                "room_id": room_id,
                "sender": requester.user.to_string(),
            },
            txn_id=txn_id,
        )

        defer.returnValue((200, {"event_id": event.event_id}))

    def on_GET(self, request, room_id, event_type, txn_id):
        return (200, "Not implemented")

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, event_type, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request, room_id, event_type, txn_id)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


# TODO: Needs unit testing for room ID + alias joins
class JoinRoomAliasServlet(ClientV1RestServlet):

    def register(self, http_server):
        # /join/$room_identifier[/$txn_id]
        PATTERNS = ("/join/(?P<room_identifier>[^/]*)")
        register_txn_path(self, PATTERNS, http_server)

    @defer.inlineCallbacks
    def on_POST(self, request, room_identifier, txn_id=None):
        requester = yield self.auth.get_user_by_req(
            request,
            allow_guest=True,
        )

        try:
            content = parse_json_object_from_request(request)
        except:
            # Turns out we used to ignore the body entirely, and some clients
            # cheekily send invalid bodies.
            content = {}

        if RoomID.is_valid(room_identifier):
            room_id = room_identifier
            try:
                remote_room_hosts = request.args["server_name"]
            except:
                remote_room_hosts = None
        elif RoomAlias.is_valid(room_identifier):
            handler = self.handlers.room_member_handler
            room_alias = RoomAlias.from_string(room_identifier)
            room_id, remote_room_hosts = yield handler.lookup_room_alias(room_alias)
            room_id = room_id.to_string()
        else:
            raise SynapseError(400, "%s was not legal room ID or room alias" % (
                room_identifier,
            ))

        yield self.handlers.room_member_handler.update_membership(
            requester=requester,
            target=requester.user,
            room_id=room_id,
            action="join",
            txn_id=txn_id,
            remote_room_hosts=remote_room_hosts,
            third_party_signed=content.get("third_party_signed", None),
        )

        defer.returnValue((200, {"room_id": room_id}))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_identifier, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request, room_identifier, txn_id)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


# TODO: Needs unit testing
class PublicRoomListRestServlet(ClientV1RestServlet):
    PATTERNS = client_path_patterns("/publicRooms$")

    @defer.inlineCallbacks
    def on_GET(self, request):
        try:
            yield self.auth.get_user_by_req(request)
        except AuthError:
            # This endpoint isn't authed, but its useful to know who's hitting
            # it if they *do* supply an access token
            pass

        handler = self.hs.get_room_list_handler()
        data = yield handler.get_aggregated_public_room_list()

        defer.returnValue((200, data))


# TODO: Needs unit testing
class RoomMemberListRestServlet(ClientV1RestServlet):
    PATTERNS = client_path_patterns("/rooms/(?P<room_id>[^/]*)/members$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        # TODO support Pagination stream API (limit/tokens)
        requester = yield self.auth.get_user_by_req(request)
        handler = self.handlers.message_handler
        events = yield handler.get_state_events(
            room_id=room_id,
            user_id=requester.user.to_string(),
        )

        chunk = []

        for event in events:
            if event["type"] != EventTypes.Member:
                continue
            chunk.append(event)

        defer.returnValue((200, {
            "chunk": chunk
        }))


# TODO: Needs better unit testing
class RoomMessageListRestServlet(ClientV1RestServlet):
    PATTERNS = client_path_patterns("/rooms/(?P<room_id>[^/]*)/messages$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)
        pagination_config = PaginationConfig.from_request(
            request, default_limit=10,
        )
        as_client_event = "raw" not in request.args
        handler = self.handlers.message_handler
        msgs = yield handler.get_messages(
            room_id=room_id,
            requester=requester,
            pagin_config=pagination_config,
            as_client_event=as_client_event
        )

        defer.returnValue((200, msgs))


# TODO: Needs unit testing
class RoomStateRestServlet(ClientV1RestServlet):
    PATTERNS = client_path_patterns("/rooms/(?P<room_id>[^/]*)/state$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)
        handler = self.handlers.message_handler
        # Get all the current state for this room
        events = yield handler.get_state_events(
            room_id=room_id,
            user_id=requester.user.to_string(),
            is_guest=requester.is_guest,
        )
        defer.returnValue((200, events))


# TODO: Needs unit testing
class RoomInitialSyncRestServlet(ClientV1RestServlet):
    PATTERNS = client_path_patterns("/rooms/(?P<room_id>[^/]*)/initialSync$")

    @defer.inlineCallbacks
    def on_GET(self, request, room_id):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)
        pagination_config = PaginationConfig.from_request(request)
        content = yield self.handlers.message_handler.room_initial_sync(
            room_id=room_id,
            requester=requester,
            pagin_config=pagination_config,
        )
        defer.returnValue((200, content))


class RoomEventContext(ClientV1RestServlet):
    PATTERNS = client_path_patterns(
        "/rooms/(?P<room_id>[^/]*)/context/(?P<event_id>[^/]*)$"
    )

    def __init__(self, hs):
        super(RoomEventContext, self).__init__(hs)
        self.clock = hs.get_clock()

    @defer.inlineCallbacks
    def on_GET(self, request, room_id, event_id):
        requester = yield self.auth.get_user_by_req(request, allow_guest=True)

        limit = int(request.args.get("limit", [10])[0])

        results = yield self.handlers.room_context_handler.get_event_context(
            requester.user,
            room_id,
            event_id,
            limit,
            requester.is_guest,
        )

        if not results:
            raise SynapseError(
                404, "Event not found.", errcode=Codes.NOT_FOUND
            )

        time_now = self.clock.time_msec()
        results["events_before"] = [
            serialize_event(event, time_now) for event in results["events_before"]
        ]
        results["event"] = serialize_event(results["event"], time_now)
        results["events_after"] = [
            serialize_event(event, time_now) for event in results["events_after"]
        ]
        results["state"] = [
            serialize_event(event, time_now) for event in results["state"]
        ]

        defer.returnValue((200, results))


class RoomForgetRestServlet(ClientV1RestServlet):
    def register(self, http_server):
        PATTERNS = ("/rooms/(?P<room_id>[^/]*)/forget")
        register_txn_path(self, PATTERNS, http_server)

    @defer.inlineCallbacks
    def on_POST(self, request, room_id, txn_id=None):
        requester = yield self.auth.get_user_by_req(
            request,
            allow_guest=False,
        )

        yield self.handlers.room_member_handler.forget(
            user=requester.user,
            room_id=room_id,
        )

        defer.returnValue((200, {}))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(
            request, room_id, txn_id
        )

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


# TODO: Needs unit testing
class RoomMembershipRestServlet(ClientV1RestServlet):

    def register(self, http_server):
        # /rooms/$roomid/[invite|join|leave]
        PATTERNS = ("/rooms/(?P<room_id>[^/]*)/"
                    "(?P<membership_action>join|invite|leave|ban|unban|kick|forget)")
        register_txn_path(self, PATTERNS, http_server)

    @defer.inlineCallbacks
    def on_POST(self, request, room_id, membership_action, txn_id=None):
        requester = yield self.auth.get_user_by_req(
            request,
            allow_guest=True,
        )

        if requester.is_guest and membership_action not in {
            Membership.JOIN,
            Membership.LEAVE
        }:
            raise AuthError(403, "Guest access not allowed")

        try:
            content = parse_json_object_from_request(request)
        except:
            # Turns out we used to ignore the body entirely, and some clients
            # cheekily send invalid bodies.
            content = {}

        if membership_action == "invite" and self._has_3pid_invite_keys(content):
            yield self.handlers.room_member_handler.do_3pid_invite(
                room_id,
                requester.user,
                content["medium"],
                content["address"],
                content["id_server"],
                requester,
                txn_id
            )
            defer.returnValue((200, {}))
            return

        target = requester.user
        if membership_action in ["invite", "ban", "unban", "kick"]:
            if "user_id" not in content:
                raise SynapseError(400, "Missing user_id key.")
            target = UserID.from_string(content["user_id"])

        yield self.handlers.room_member_handler.update_membership(
            requester=requester,
            target=target,
            room_id=room_id,
            action=membership_action,
            txn_id=txn_id,
            third_party_signed=content.get("third_party_signed", None),
        )

        defer.returnValue((200, {}))

    def _has_3pid_invite_keys(self, content):
        for key in {"id_server", "medium", "address"}:
            if key not in content:
                return False
        return True

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, membership_action, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(
            request, room_id, membership_action, txn_id
        )

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


class RoomRedactEventRestServlet(ClientV1RestServlet):
    def register(self, http_server):
        PATTERNS = ("/rooms/(?P<room_id>[^/]*)/redact/(?P<event_id>[^/]*)")
        register_txn_path(self, PATTERNS, http_server)

    @defer.inlineCallbacks
    def on_POST(self, request, room_id, event_id, txn_id=None):
        requester = yield self.auth.get_user_by_req(request)
        content = parse_json_object_from_request(request)

        msg_handler = self.handlers.message_handler
        event = yield msg_handler.create_and_send_nonmember_event(
            requester,
            {
                "type": EventTypes.Redaction,
                "content": content,
                "room_id": room_id,
                "sender": requester.user.to_string(),
                "redacts": event_id,
            },
            txn_id=txn_id,
        )

        defer.returnValue((200, {"event_id": event.event_id}))

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, event_id, txn_id):
        try:
            defer.returnValue(
                self.txns.get_client_transaction(request, txn_id)
            )
        except KeyError:
            pass

        response = yield self.on_POST(request, room_id, event_id, txn_id)

        self.txns.store_client_transaction(request, txn_id, response)
        defer.returnValue(response)


class RoomTypingRestServlet(ClientV1RestServlet):
    PATTERNS = client_path_patterns(
        "/rooms/(?P<room_id>[^/]*)/typing/(?P<user_id>[^/]*)$"
    )

    def __init__(self, hs):
        super(RoomTypingRestServlet, self).__init__(hs)
        self.presence_handler = hs.get_presence_handler()
        self.typing_handler = hs.get_typing_handler()

    @defer.inlineCallbacks
    def on_PUT(self, request, room_id, user_id):
        requester = yield self.auth.get_user_by_req(request)

        room_id = urllib.unquote(room_id)
        target_user = UserID.from_string(urllib.unquote(user_id))

        content = parse_json_object_from_request(request)

        yield self.presence_handler.bump_presence_active_time(requester.user)

        if content["typing"]:
            yield self.typing_handler.started_typing(
                target_user=target_user,
                auth_user=requester.user,
                room_id=room_id,
                timeout=content.get("timeout", 30000),
            )
        else:
            yield self.typing_handler.stopped_typing(
                target_user=target_user,
                auth_user=requester.user,
                room_id=room_id,
            )

        defer.returnValue((200, {}))


class SearchRestServlet(ClientV1RestServlet):
    PATTERNS = client_path_patterns(
        "/search$"
    )

    @defer.inlineCallbacks
    def on_POST(self, request):
        requester = yield self.auth.get_user_by_req(request)

        content = parse_json_object_from_request(request)

        batch = request.args.get("next_batch", [None])[0]
        results = yield self.handlers.search_handler.search(
            requester.user,
            content,
            batch,
        )

        defer.returnValue((200, results))


def register_txn_path(servlet, regex_string, http_server, with_get=False):
    """Registers a transaction-based path.

    This registers two paths:
        PUT regex_string/$txnid
        POST regex_string

    Args:
        regex_string (str): The regex string to register. Must NOT have a
        trailing $ as this string will be appended to.
        http_server : The http_server to register paths with.
        with_get: True to also register respective GET paths for the PUTs.
    """
    http_server.register_paths(
        "POST",
        client_path_patterns(regex_string + "$"),
        servlet.on_POST
    )
    http_server.register_paths(
        "PUT",
        client_path_patterns(regex_string + "/(?P<txn_id>[^/]*)$"),
        servlet.on_PUT
    )
    if with_get:
        http_server.register_paths(
            "GET",
            client_path_patterns(regex_string + "/(?P<txn_id>[^/]*)$"),
            servlet.on_GET
        )


def register_servlets(hs, http_server):
    RoomStateEventRestServlet(hs).register(http_server)
    RoomCreateRestServlet(hs).register(http_server)
    RoomMemberListRestServlet(hs).register(http_server)
    RoomMessageListRestServlet(hs).register(http_server)
    JoinRoomAliasServlet(hs).register(http_server)
    RoomForgetRestServlet(hs).register(http_server)
    RoomMembershipRestServlet(hs).register(http_server)
    RoomSendEventRestServlet(hs).register(http_server)
    PublicRoomListRestServlet(hs).register(http_server)
    RoomStateRestServlet(hs).register(http_server)
    RoomInitialSyncRestServlet(hs).register(http_server)
    RoomRedactEventRestServlet(hs).register(http_server)
    RoomTypingRestServlet(hs).register(http_server)
    SearchRestServlet(hs).register(http_server)
    RoomEventContext(hs).register(http_server)
