"""Websockets Client for asyncio."""

import asyncio
import logging
from contextlib import suppress
from ssl import SSLContext
from typing import (
    Any,
    AsyncGenerator,
    Collection,
    Dict,
    Optional,
    Tuple,
    Union,
    Mapping,
)

import aiohttp
from aiohttp import hdrs, BasicAuth, Fingerprint, WSMsgType
from aiohttp.typedefs import LooseHeaders, StrOrURL
from graphql import DocumentNode, ExecutionResult, print_ast
from multidict import CIMultiDict, CIMultiDictProxy

from gql.transport.async_transport import AsyncTransport
from gql.transport.exceptions import (
    TransportAlreadyConnected,
    TransportClosed,
    TransportProtocolError,
    TransportQueryError,
    TransportServerError,
)
from gql.transport.websockets_base import ListenerQueue

try:
    from json.decoder import JSONDecodeError
except ImportError:
    from simplejson import JSONDecodeError

log = logging.getLogger("gql.transport.aiohttp_websockets")


class AIOHTTPWebsocketsTransport(AsyncTransport):

    # This transport supports two subprotocols and will autodetect the
    # subprotocol supported on the server
    APOLLO_SUBPROTOCOL: str = "graphql-ws"
    GRAPHQLWS_SUBPROTOCOL: str = "graphql-transport-ws"

    def __init__(
        self,
        url: StrOrURL,
        *,
        method: str = hdrs.METH_GET,
        protocols: Collection[str] = (),
        timeout: float = 10.0,
        receive_timeout: Optional[float] = None,
        autoclose: bool = True,
        autoping: bool = True,
        heartbeat: Optional[float] = None,
        auth: Optional[BasicAuth] = None,
        origin: Optional[str] = None,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[LooseHeaders] = None,
        proxy: Optional[StrOrURL] = None,
        proxy_auth: Optional[BasicAuth] = None,
        ssl: Union[SSLContext, bool, Fingerprint] = True,
        ssl_context: Optional[SSLContext] = None,
        verify_ssl: Optional[bool] = True,
        proxy_headers: Optional[LooseHeaders] = None,
        compress: int = 0,
        max_msg_size: int = 4 * 1024 * 1024,
        connect_timeout: Optional[Union[int, float]] = 10,
        close_timeout: Optional[Union[int, float]] = 10,
        ack_timeout: Optional[Union[int, float]] = 10,
        keep_alive_timeout: Optional[Union[int, float]] = None,
        init_payload: Dict[str, Any] = {},
        ping_interval: Optional[Union[int, float]] = None,
        pong_timeout: Optional[Union[int, float]] = None,
        answer_pings: bool = True,
    ) -> None:
        self.url: StrOrURL = url
        self.headers: Optional[LooseHeaders] = headers
        self.auth: Optional[BasicAuth] = auth
        self.autoclose: bool = autoclose
        self.autoping: bool = autoping
        self.compress: int = compress
        self.heartbeat: Optional[float] = heartbeat
        self.max_msg_size: int = max_msg_size
        self.method: str = method
        self.origin: Optional[str] = origin
        self.params: Optional[Mapping[str, str]] = params
        self.protocols: Collection[str] = protocols
        self.proxy: Optional[StrOrURL] = proxy
        self.proxy_auth: Optional[BasicAuth] = proxy_auth
        self.proxy_headers: Optional[LooseHeaders] = proxy_headers
        self.receive_timeout: Optional[float] = receive_timeout
        self.ssl: Union[SSLContext, bool, Fingerprint] = ssl
        self.ssl_context: Optional[SSLContext] = ssl_context
        self.timeout: float = timeout
        self.verify_ssl: Optional[bool] = verify_ssl
        self.init_payload: Dict[str, Any] = init_payload

        self.connect_timeout: Optional[Union[int, float]] = connect_timeout
        self.close_timeout: Optional[Union[int, float]] = close_timeout
        self.ack_timeout: Optional[Union[int, float]] = ack_timeout
        self.keep_alive_timeout: Optional[Union[int, float]] = keep_alive_timeout
        self._next_keep_alive_message: asyncio.Event = asyncio.Event()
        self._next_keep_alive_message.set()

        self.session: Optional[aiohttp.ClientSession] = None
        self.websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self.next_query_id: int = 1
        self.listeners: Dict[int, ListenerQueue] = {}
        self._connecting: bool = False
        self.response_headers: Optional[CIMultiDictProxy[str]] = None

        self.receive_data_task: Optional[asyncio.Future] = None
        self.check_keep_alive_task: Optional[asyncio.Future] = None
        self.close_task: Optional[asyncio.Future] = None

        self._wait_closed: asyncio.Event = asyncio.Event()
        self._wait_closed.set()

        self._no_more_listeners: asyncio.Event = asyncio.Event()
        self._no_more_listeners.set()

        self.payloads: Dict[str, Any] = {}

        self.ping_interval: Optional[Union[int, float]] = ping_interval
        self.pong_timeout: Optional[Union[int, float]]
        self.answer_pings: bool = answer_pings

        if ping_interval is not None:
            if pong_timeout is None:
                self.pong_timeout = ping_interval / 2
            else:
                self.pong_timeout = pong_timeout

        self.send_ping_task: Optional[asyncio.Future] = None

        self.ping_received: asyncio.Event = asyncio.Event()
        """ping_received is an asyncio Event which will fire  each time
        a ping is received with the graphql-ws protocol"""

        self.pong_received: asyncio.Event = asyncio.Event()
        """pong_received is an asyncio Event which will fire  each time
        a pong is received with the graphql-ws protocol"""

        self.supported_subprotocols: Collection[str] = protocols or (
            self.APOLLO_SUBPROTOCOL,
            self.GRAPHQLWS_SUBPROTOCOL,
        )
        self.close_exception: Optional[Exception] = None

    def _parse_answer_graphqlws(
        self, answer: Dict[str, Any]
    ) -> Tuple[str, Optional[int], Optional[ExecutionResult]]:
        """Parse the answer received from the server if the server supports the
        graphql-ws protocol.

        Returns a list consisting of:
            - the answer_type (between:
              'connection_ack', 'ping', 'pong', 'data', 'error', 'complete')
            - the answer id (Integer) if received or None
            - an execution Result if the answer_type is 'data' or None

        Differences with the apollo websockets protocol (superclass):
            - the "data" message is now called "next"
            - the "stop" message is now called "complete"
            - there is no connection_terminate or connection_error messages
            - instead of a unidirectional keep-alive (ka) message from server to client,
              there is now the possibility to send bidirectional ping/pong messages
            - connection_ack has an optional payload
            - the 'error' answer type returns a list of errors instead of a single error
        """

        answer_type: str = ""
        answer_id: Optional[int] = None
        execution_result: Optional[ExecutionResult] = None

        try:
            answer_type = str(answer.get("type"))

            if answer_type in ["next", "error", "complete"]:
                answer_id = int(str(answer.get("id")))

                if answer_type == "next" or answer_type == "error":

                    payload = answer.get("payload")

                    if answer_type == "next":

                        if not isinstance(payload, dict):
                            raise ValueError("payload is not a dict")

                        if "errors" not in payload and "data" not in payload:
                            raise ValueError(
                                "payload does not contain 'data' or 'errors' fields"
                            )

                        execution_result = ExecutionResult(
                            errors=payload.get("errors"),
                            data=payload.get("data"),
                            extensions=payload.get("extensions"),
                        )

                        # Saving answer_type as 'data' to be understood with superclass
                        answer_type = "data"

                    elif answer_type == "error":

                        if not isinstance(payload, list):
                            raise ValueError("payload is not a list")

                        raise TransportQueryError(
                            str(payload[0]), query_id=answer_id, errors=payload
                        )

            elif answer_type in ["ping", "pong", "connection_ack"]:
                self.payloads[answer_type] = answer.get("payload", None)

            else:
                raise ValueError

            if self.check_keep_alive_task is not None:
                self._next_keep_alive_message.set()

        except ValueError as e:
            raise TransportProtocolError(
                f"Server did not return a GraphQL result: {answer}"
            ) from e

        return answer_type, answer_id, execution_result

    def _parse_answer_apollo(
        self, answer: Dict[str, Any]
    ) -> Tuple[str, Optional[int], Optional[ExecutionResult]]:
        """Parse the answer received from the server if the server supports the
        apollo websockets protocol.

        Returns a list consisting of:
            - the answer_type (between:
              'connection_ack', 'ka', 'connection_error', 'data', 'error', 'complete')
            - the answer id (Integer) if received or None
            - an execution Result if the answer_type is 'data' or None
        """

        answer_type: str = ""
        answer_id: Optional[int] = None
        execution_result: Optional[ExecutionResult] = None

        try:
            answer_type = str(answer.get("type"))

            if answer_type in ["data", "error", "complete"]:
                answer_id = int(str(answer.get("id")))

                if answer_type == "data" or answer_type == "error":

                    payload = answer.get("payload")

                    if not isinstance(payload, dict):
                        raise ValueError("payload is not a dict")

                    if answer_type == "data":

                        if "errors" not in payload and "data" not in payload:
                            raise ValueError(
                                "payload does not contain 'data' or 'errors' fields"
                            )

                        execution_result = ExecutionResult(
                            errors=payload.get("errors"),
                            data=payload.get("data"),
                            extensions=payload.get("extensions"),
                        )

                    elif answer_type == "error":

                        raise TransportQueryError(
                            str(payload), query_id=answer_id, errors=[payload]
                        )

            elif answer_type == "ka":
                # Keep-alive message
                if self.check_keep_alive_task is not None:
                    self._next_keep_alive_message.set()
            elif answer_type == "connection_ack":
                pass
            elif answer_type == "connection_error":
                error_payload = answer.get("payload")
                raise TransportServerError(f"Server error: '{repr(error_payload)}'")
            else:
                raise ValueError

        except ValueError as e:
            raise TransportProtocolError(
                f"Server did not return a GraphQL result: {answer}"
            ) from e

        return answer_type, answer_id, execution_result

    def _parse_answer(
        self, answer: Dict[str, Any]
    ) -> Tuple[str, Optional[int], Optional[ExecutionResult]]:
        """Parse the answer received from the server depending on
        the detected subprotocol.
        """
        if self.subprotocol == self.GRAPHQLWS_SUBPROTOCOL:
            return self._parse_answer_graphqlws(answer)

        return self._parse_answer_apollo(answer)

    async def _wait_ack(self) -> None:
        """Wait for the connection_ack message. Keep alive messages are ignored"""

        while True:
            init_answer = await self._receive()

            answer_type, _, _ = self._parse_answer(init_answer)

            if answer_type == "connection_ack":
                return

            if answer_type != "ka":
                raise TransportProtocolError(
                    "Websocket server did not return a connection ack"
                )

    async def _send_init_message_and_wait_ack(self) -> None:
        """Send init message to the provided websocket and wait for the connection ACK.

        If the answer is not a connection_ack message, we will return an Exception.
        """

        init_message = {"type": "connection_init", "payload": self.init_payload}

        await self._send(init_message)

        # Wait for the connection_ack message or raise a TimeoutError
        await asyncio.wait_for(self._wait_ack(), self.ack_timeout)

    async def _initialize(self):
        await self._send_init_message_and_wait_ack()

    async def _stop_listener(self, query_id: int):
        """Hook to stop to listen to a specific query.
        Will send a stop message in some subclasses.
        """
        pass  # pragma: no cover

    async def _after_connect(self):
        if self.websocket is None:
            raise TransportClosed("WebSocket connection is closed")

        # Find the backend subprotocol returned in the response headers
        # TODO: find the equivalent of response_headers in aiohttp websocket response
        subprotocol = self.websocket.protocol
        try:
            self.subprotocol = subprotocol
        except KeyError:
            # If the server does not send the subprotocol header, using
            # the apollo subprotocol by default
            self.subprotocol = self.APOLLO_SUBPROTOCOL

        log.debug(f"backend subprotocol returned: {self.subprotocol!r}")

    async def send_ping(self, payload: Optional[Any] = None) -> None:
        """Send a ping message for the graphql-ws protocol"""

        ping_message = {"type": "ping"}

        if payload is not None:
            ping_message["payload"] = payload

        await self._send(ping_message)

    async def _send_ping_coro(self) -> None:
        """Coroutine to periodically send a ping from the client to the backend.

        Only used for the graphql-ws protocol.

        Send a ping every ping_interval seconds.
        Close the connection if a pong is not received within pong_timeout seconds.
        """

        assert self.ping_interval is not None

        try:
            while True:
                await asyncio.sleep(self.ping_interval)

                await self.send_ping()

                await asyncio.wait_for(self.pong_received.wait(), self.pong_timeout)

                # Reset for the next iteration
                self.pong_received.clear()

        except asyncio.TimeoutError:
            # No pong received in the appriopriate time, close with error
            # If the timeout happens during a close already in progress, do nothing
            if self.close_task is None:
                await self._fail(
                    TransportServerError(
                        f"No pong received after {self.pong_timeout!r} seconds"
                    ),
                    clean_close=False,
                )

    async def _after_initialize(self):

        # If requested, create a task to send periodic pings to the backend
        if (
            self.subprotocol == self.GRAPHQLWS_SUBPROTOCOL
            and self.ping_interval is not None
        ):

            self.send_ping_task = asyncio.ensure_future(self._send_ping_coro())

    async def _close_hook(self):
        """Hook to add custom code for subclasses for the connection close"""
        pass  # pragma: no cover

    async def _connection_terminate(self):
        """Hook to add custom code for subclasses after the initialization
        has been done.
        """
        pass  # pragma: no cover

    async def _send_query(
        self,
        document: DocumentNode,
        variable_values: Optional[Dict[str, Any]] = None,
        operation_name: Optional[str] = None,
    ) -> int:
        """Send a query to the provided websocket connection.

        We use an incremented id to reference the query.

        Returns the used id for this query.
        """

        query_id = self.next_query_id
        self.next_query_id += 1

        payload: Dict[str, Any] = {"query": print_ast(document)}
        if variable_values:
            payload["variables"] = variable_values
        if operation_name:
            payload["operationName"] = operation_name

        query_type = "start"

        if self.subprotocol == self.GRAPHQLWS_SUBPROTOCOL:
            query_type = "subscribe"

        query = {"id": str(query_id), "type": query_type, "payload": payload}

        await self._send(query)

        return query_id

    async def _send(self, message: Dict[str, Any]) -> None:
        """Send the provided message to the websocket connection and log the message"""

        if self.websocket is None:
            raise TransportClosed("WebSocket connection is closed")

        try:
            await self.websocket.send_json(message)
            log.info(">>> %s", message)
        except ConnectionResetError as e:
            await self._fail(e, clean_close=False)
            raise e

    async def _receive(self) -> Dict[str, Any]:
        log.debug("Entering _receive()")

        if self.websocket is None:
            raise TransportClosed("WebSocket connection is closed")

        try:
            answer = await self.websocket.receive_json()
        except TypeError as e:
            answer = await self.websocket.receive()
            if answer.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                self._fail(e, clean_close=True)
                raise ConnectionResetError
            else:
                self._fail(e, clean_close=False)
        except JSONDecodeError as e:
            self._fail(e)

        log.info("<<< %s", answer)

        log.debug("Exiting _receive()")

        return answer

    def _remove_listener(self, query_id) -> None:
        """After exiting from a subscription, remove the listener and
        signal an event if this was the last listener for the client.
        """
        if query_id in self.listeners:
            del self.listeners[query_id]

        remaining = len(self.listeners)
        log.debug(f"listener {query_id} deleted, {remaining} remaining")

        if remaining == 0:
            self._no_more_listeners.set()

    async def _check_ws_liveness(self) -> None:
        """Coroutine which will periodically check the liveness of the connection
        through keep-alive messages
        """

        try:
            while True:
                await asyncio.wait_for(
                    self._next_keep_alive_message.wait(), self.keep_alive_timeout
                )

                # Reset for the next iteration
                self._next_keep_alive_message.clear()

        except asyncio.TimeoutError:
            # No keep-alive message in the appriopriate interval, close with error
            # while trying to notify the server of a proper close (in case
            # the keep-alive interval of the client or server was not aligned
            # the connection still remains)

            # If the timeout happens during a close already in progress, do nothing
            if self.close_task is None:
                await self._fail(
                    TransportServerError(
                        "No keep-alive message has been received within "
                        "the expected interval ('keep_alive_timeout' parameter)"
                    ),
                    clean_close=False,
                )

        except asyncio.CancelledError:
            # The client is probably closing, handle it properly
            pass

    async def _handle_answer(
        self,
        answer_type: str,
        answer_id: Optional[int],
        execution_result: Optional[ExecutionResult],
    ) -> None:

        try:
            # Put the answer in the queue
            if answer_id is not None:
                await self.listeners[answer_id].put((answer_type, execution_result))
        except KeyError:
            # Do nothing if no one is listening to this query_id.
            pass

    async def _receive_data_loop(self) -> None:
        """Main asyncio task which will listen to the incoming messages and will
        call the parse_answer and handle_answer methods of the subclass."""
        log.debug("Entering _receive_data_loop()")

        try:
            while True:

                # Wait the next answer from the websocket server
                try:
                    answer = await self._receive()
                except (ConnectionResetError, TransportProtocolError) as e:
                    await self._fail(e, clean_close=False)
                    break
                except TransportClosed:
                    break

                # Parse the answer
                try:
                    answer_type, answer_id, execution_result = self._parse_answer(
                        answer
                    )
                except TransportQueryError as e:
                    # Received an exception for a specific query
                    # ==> Add an exception to this query queue
                    # The exception is raised for this specific query,
                    # but the transport is not closed.
                    assert isinstance(
                        e.query_id, int
                    ), "TransportQueryError should have a query_id defined here"
                    try:
                        await self.listeners[e.query_id].set_exception(e)
                    except KeyError:
                        # Do nothing if no one is listening to this query_id
                        pass

                    continue

                except (TransportServerError, TransportProtocolError) as e:
                    # Received a global exception for this transport
                    # ==> close the transport
                    # The exception will be raised for all current queries.
                    await self._fail(e, clean_close=False)
                    break

                await self._handle_answer(answer_type, answer_id, execution_result)

        finally:
            log.debug("Exiting _receive_data_loop()")

    async def connect(self) -> None:
        log.debug("connect: starting")

        if self.session is None:
            self.session = aiohttp.ClientSession()

        if self.websocket is None and not self._connecting:
            self._connecting = True

            try:
                self.websocket = await self.session.ws_connect(
                    method=self.method,
                    url=self.url,
                    headers=self.headers,
                    auth=self.auth,
                    autoclose=self.autoclose,
                    autoping=self.autoping,
                    compress=self.compress,
                    heartbeat=self.heartbeat,
                    max_msg_size=self.max_msg_size,
                    origin=self.origin,
                    params=self.params,
                    protocols=self.protocols,
                    proxy=self.proxy,
                    proxy_auth=self.proxy_auth,
                    proxy_headers=self.proxy_headers,
                    receive_timeout=self.receive_timeout,
                    ssl=self.ssl,
                    ssl_context=None,
                    timeout=self.timeout,
                    verify_ssl=self.verify_ssl,
                )
            finally:
                self._connecting = False

            try:
                self.response_headers = self.websocket._response.headers
            except AttributeError:
                self.response_headers = CIMultiDictProxy(CIMultiDict())

            await self._after_connect()

            self.next_query_id = 1
            self.close_exception = None
            self._wait_closed.clear()

            # Send the init message and wait for the ack from the server
            # Note: This should generate a TimeoutError
            # if no ACKs are received within the ack_timeout
            try:
                await self._initialize()
            except ConnectionResetError as e:
                raise e
            except (TransportProtocolError, asyncio.TimeoutError) as e:
                await self._fail(e, clean_close=False)
                raise e

            # Run the after_init hook of the subclass
            await self._after_initialize()

            # If specified, create a task to check liveness of the connection
            # through keep-alive messages
            if self.keep_alive_timeout is not None:
                self.check_keep_alive_task = asyncio.ensure_future(
                    self._check_ws_liveness()
                )

            # Create a task to listen to the incoming websocket messages
            self.receive_data_task = asyncio.ensure_future(self._receive_data_loop())

        else:
            raise TransportAlreadyConnected("Transport is already connected")

        log.debug("connect: done")

    async def _clean_close(self) -> None:
        """Coroutine which will:

        - send stop messages for each active subscription to the server
        - send the connection terminate message
        """

        # Send 'stop' message for all current queries
        for query_id, listener in self.listeners.items():

            if listener.send_stop:
                await self._stop_listener(query_id)
                listener.send_stop = False

        # Wait that there is no more listeners (we received 'complete' for all queries)
        try:
            await asyncio.wait_for(self._no_more_listeners.wait(), self.close_timeout)
        except asyncio.TimeoutError:  # pragma: no cover
            log.debug("Timer close_timeout fired")

        # Calling the subclass hook
        await self._connection_terminate()

    async def _close_coro(self, e: Exception, clean_close: bool = True) -> None:
        """Coroutine which will:

        - do a clean_close if possible:
            - send stop messages for each active query to the server
            - send the connection terminate message
        - close the websocket connection
        - send the exception to all the remaining listeners
        """

        log.debug("_close_coro: starting")

        try:

            # We should always have an active websocket connection here
            assert self.websocket is not None

            # Properly shut down liveness checker if enabled
            if self.check_keep_alive_task is not None:
                # More info: https://stackoverflow.com/a/43810272/1113207
                self.check_keep_alive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.check_keep_alive_task

            # Calling the subclass close hook
            await self._close_hook()

            # Saving exception to raise it later if trying to use the transport
            # after it has already closed.
            self.close_exception = e

            if clean_close:
                log.debug("_close_coro: starting clean_close")
                try:
                    await self._clean_close()
                except Exception as exc:  # pragma: no cover
                    log.warning("Ignoring exception in _clean_close: " + repr(exc))

            log.debug("_close_coro: sending exception to listeners")

            # Send an exception to all remaining listeners
            for query_id, listener in self.listeners.items():
                await listener.set_exception(e)

            log.debug("_close_coro: close websocket connection")

            await self.websocket.close()

            log.debug("_close_coro: websocket connection closed")

        except Exception as exc:  # pragma: no cover
            log.warning("Exception catched in _close_coro: " + repr(exc))

        finally:

            log.debug("_close_coro: start cleanup")

            self.websocket = None
            self.close_task = None
            self.check_keep_alive_task = None
            self._wait_closed.set()

        log.debug("_close_coro: exiting")

    async def _fail(self, e: Exception, clean_close: bool = True) -> None:
        log.debug("_fail: starting with exception: " + repr(e))

        if self.close_task is None:

            if self.websocket is None:
                log.debug("_fail started with self.websocket == None -> already closed")
            else:
                self.close_task = asyncio.shield(
                    asyncio.ensure_future(self._close_coro(e, clean_close=clean_close))
                )
        else:
            log.debug(
                "close_task is not None in _fail. Previous exception is: "
                + repr(self.close_exception)
                + " New exception is: "
                + repr(e)
            )

    async def close(self) -> None:
        log.debug("close: starting")

        await self._fail(TransportClosed("Websocket GraphQL transport closed by user"))
        await self.wait_closed()

        log.debug("close: done")

    async def wait_closed(self) -> None:
        log.debug("wait_close: starting")

        await self._wait_closed.wait()

        log.debug("wait_close: done")

    async def execute(
        self,
        document: DocumentNode,
        variable_values: Optional[Dict[str, Any]] = None,
        operation_name: Optional[str] = None,
    ) -> ExecutionResult:
        """Execute the provided document AST against the configured remote server
        using the current session.

        Send a query but close the async generator as soon as we have the first answer.

        The result is sent as an ExecutionResult object.
        """
        first_result = None

        generator = self.subscribe(
            document, variable_values, operation_name, send_stop=False
        )

        async for result in generator:
            first_result = result

            # Note: we need to run generator.aclose() here or the finally block in
            # the subscribe will not be reached in pypy3 (python version 3.6.1)
            await generator.aclose()

            break

        if first_result is None:
            raise TransportQueryError(
                "Query completed without any answer received from the server"
            )

        return first_result

    async def subscribe(
        self,
        document: DocumentNode,
        variable_values: Optional[Dict[str, Any]] = None,
        operation_name: Optional[str] = None,
        send_stop: Optional[bool] = True,
    ) -> AsyncGenerator[ExecutionResult, None]:
        """Send a query and receive the results using a python async generator.

        The query can be a graphql query, mutation or subscription.

        The results are sent as an ExecutionResult object.
        """

        # Send the query and receive the id
        query_id: int = await self._send_query(
            document, variable_values, operation_name
        )

        # Create a queue to receive the answers for this query_id
        listener = ListenerQueue(query_id, send_stop=(send_stop is True))
        self.listeners[query_id] = listener

        # We will need to wait at close for this query to clean properly
        self._no_more_listeners.clear()

        try:
            # Loop over the received answers
            while True:

                # Wait for the answer from the queue of this query_id
                # This can raise a TransportError or ConnectionClosed exception.
                answer_type, execution_result = await listener.get()

                # If the received answer contains data,
                # Then we will yield the results back as an ExecutionResult object
                if execution_result is not None:
                    yield execution_result

                # If we receive a 'complete' answer from the server,
                # Then we will end this async generator output without errors
                elif answer_type == "complete":
                    log.debug(
                        f"Complete received for query {query_id} --> exit without error"
                    )
                    break

        except (asyncio.CancelledError, GeneratorExit) as e:
            log.debug(f"Exception in subscribe: {e!r}")
            if listener.send_stop:
                await self._stop_listener(query_id)
                listener.send_stop = False

        finally:
            log.debug(f"In subscribe finally for query_id {query_id}")
            self._remove_listener(query_id)
