# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import dataclasses
import json
from abc import abstractmethod
from enum import Enum
from json.decoder import JSONDecodeError
from typing import Any, BinaryIO, Dict, Optional, Union, Sequence, Mapping


JSON = Dict[str, Any]


class LanguageServerMessageType(Enum):
    """Message type for an LSP warning message."""

    WARNING = 2
    INFORMATION = 3


class JSONRPCException(Exception):
    pass


class JSONRPC:
    @abstractmethod
    def json(self) -> JSON:
        raise NotImplementedError

    def serialize(self) -> str:
        return json.dumps(self.json())


def _verify_json_rpc_version(json: JSON) -> None:
    json_rpc_version = json.get("jsonrpc")
    if json_rpc_version is None:
        raise JSONRPCException(f"Required field `jsonrpc` is missing: {json}")
    if json_rpc_version != "2.0":
        raise JSONRPCException(
            f"`jsonrpc` is expected to be '2.0' but got '{json_rpc_version}'"
        )


def _parse_json_rpc_id(json: JSON) -> Union[int, str, None]:
    id = json.get("id")
    if id is not None and not isinstance(id, int) and not isinstance(id, str):
        raise JSONRPCException(
            f"Request ID must be either an integer or string but got {id}"
        )
    return id


@dataclasses.dataclass(frozen=True)
class ByPositionParameters:
    values: Sequence[object] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class ByNameParameters:
    values: Mapping[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Request(JSONRPC):
    method: str
    id: Union[int, str, None] = None
    parameters: Union[ByPositionParameters, ByNameParameters, None] = None

    def json(self) -> JSON:
        parameters = self.parameters
        return {
            "jsonrpc": "2.0",
            "method": self.method,
            **({"id": self.id} if self.id is not None else {}),
            **({"params": parameters.values} if parameters is not None else {}),
        }

    @staticmethod
    def from_json(request_json: JSON) -> "Request":
        """
        Parse a given JSON into a JSON-RPC request.
        Raises JSONRPCException if the JSON body is malformed.
        """
        _verify_json_rpc_version(request_json)

        method = request_json.get("method")
        if method is None:
            raise JSONRPCException(
                f"Required field `method` is missing: {request_json}"
            )
        if not isinstance(method, str):
            raise JSONRPCException(
                f"`method` is expected to be a string but got {method}"
            )

        raw_parameters = request_json.get("params")
        if raw_parameters is None:
            parameters = None
        elif isinstance(raw_parameters, list):
            parameters = ByPositionParameters(raw_parameters)
        elif isinstance(raw_parameters, dict):
            parameters = ByNameParameters(raw_parameters)
        else:
            raise JSONRPCException(
                f"Cannot parse request parameter JSON: {raw_parameters}"
            )

        id = _parse_json_rpc_id(request_json)
        return Request(method=method, id=id, parameters=parameters)

    @staticmethod
    def from_string(request_string: str) -> "Request":
        """
        Parse a given string into a JSON-RPC request.
        Raises JSONRPCException if the parsing fails.
        """
        try:
            request_json = json.loads(request_string)
            return Request.from_json(request_json)
        except JSONDecodeError as error:
            message = f"Cannot parse string into JSON: {error}"
            raise JSONRPCException(message) from error


@dataclasses.dataclass(frozen=True)
class Response(JSONRPC):
    id: Union[int, str, None]

    @staticmethod
    def from_json(response_json: JSON) -> "Response":
        """
        Parse a given JSON into a JSON-RPC response.
        Raises JSONRPCException if the JSON body is malformed.
        """
        if "result" in response_json:
            return SuccessResponse.from_json(response_json)
        elif "error" in response_json:
            return ErrorResponse.from_json(response_json)
        else:
            raise JSONRPCException(
                "Either `result` or `error` must be presented in JSON-RPC "
                f"responses. Got {response_json}."
            )

    @staticmethod
    def from_string(response_string: str) -> "Response":
        """
        Parse a given string into a JSON-RPC response.
        Raises JSONRPCException if the parsing fails.
        """
        try:
            response_json = json.loads(response_string)
            return Response.from_json(response_json)
        except JSONDecodeError as error:
            message = f"Cannot parse string into JSON: {error}"
            raise JSONRPCException(message) from error


@dataclasses.dataclass(frozen=True)
class SuccessResponse(Response):
    result: object

    def json(self) -> JSON:
        return {
            "jsonrpc": "2.0",
            **({"id": self.id} if self.id is not None else {}),
            "result": self.result,
        }

    @staticmethod
    def from_json(response_json: JSON) -> "SuccessResponse":
        """
        Parse a given JSON into a JSON-RPC success response.
        Raises JSONRPCException if the JSON body is malformed.
        """
        _verify_json_rpc_version(response_json)

        result = response_json.get("result")
        if result is None:
            raise JSONRPCException(
                f"Required field `result` is missing: {response_json}"
            )

        # FIXME: The `id` field is required for the respnose, but we can't
        # enforce it right now since the Pyre server may emit id-less responses
        # and that has to be fixed first.
        id = _parse_json_rpc_id(response_json)
        return SuccessResponse(id=id, result=result)


@dataclasses.dataclass(frozen=True)
class ErrorResponse(Response):
    code: int
    message: str = ""
    data: Optional[object] = None

    def json(self) -> JSON:
        return {
            "jsonrpc": "2.0",
            **({"id": self.id} if self.id is not None else {}),
            "error": {
                "code": self.code,
                "message": self.message,
                **({"data": self.data} if self.data is not None else {}),
            },
        }

    @staticmethod
    def from_json(response_json: JSON) -> "ErrorResponse":
        """
        Parse a given JSON into a JSON-RPC error response.
        Raises JSONRPCException if the JSON body is malformed.
        """
        _verify_json_rpc_version(response_json)

        error = response_json.get("error")
        if error is None:
            raise JSONRPCException(
                f"Required field `error` is missing: {response_json}"
            )
        if not isinstance(error, dict):
            raise JSONRPCException(f"`error` must be a dict but got {error}")

        code = error.get("code")
        if code is None:
            raise JSONRPCException(
                f"Required field `error.code` is missing: {response_json}"
            )
        if not isinstance(code, int):
            raise JSONRPCException(
                f"`error.code` is expected to be an int but got {code}"
            )

        message = error.get("message", "")
        if not isinstance(message, str):
            raise JSONRPCException(
                f"`error.message` is expected to be a string but got {message}"
            )

        data = error.get("data")
        # FIXME: The `id` field is required for the respnose, but we can't
        # enforce it right now since the Pyre server may emit id-less responses
        # and that has to be fixed first.
        id = _parse_json_rpc_id(response_json)
        return ErrorResponse(id=id, code=code, message=message, data=data)


def write_lsp_request(file: BinaryIO, request: Request) -> bool:
    request_string = request.serialize()
    length = len(request_string.encode("utf-8"))
    payload = f"Content-Length: {length}\r\n\r\n{request_string}".encode("utf-8")
    try:
        file.write(payload)
        file.flush()
        return True
    except (ValueError, OSError):
        return False


def parse_content_length(line: bytes) -> Optional[int]:
    if line.startswith(b"Content-Length:"):
        length = line.split(b"Content-Length:")[1].strip()
        try:
            return int(length)
        except ValueError:
            return None
    return None


def _read_payload(file: BinaryIO) -> Optional[JSON]:
    try:
        line = file.readline()
        length = parse_content_length(line)
        if not length:
            return None

        # Read header lines until the empty line
        while line.strip():
            line = file.readline()

        body = file.read(length)
        return json.loads(body.decode("utf-8"))
    except (ValueError, OSError, JSONDecodeError):
        return None


def read_lsp_request(file: BinaryIO) -> Request:
    payload = _read_payload(file)
    if payload is None:
        raise JSONRPCException("Payload reading failed.")
    return Request.from_json(payload)


def read_lsp_response(file: BinaryIO) -> Response:
    payload = _read_payload(file)
    if not payload:
        raise JSONRPCException("Received empty response.")
    return Response.from_json(payload)


def perform_handshake(
    input_file: BinaryIO, output_file: BinaryIO, client_version: str
) -> None:
    server_handshake = read_lsp_request(input_file)
    if server_handshake.method == "handshake/server":
        server_handshake_parameters = server_handshake.parameters
        if isinstance(server_handshake_parameters, ByNameParameters):
            server_version = server_handshake_parameters.values.get("version")
            if server_version != client_version:
                raise ValueError(
                    "Version mismatch. Server has version `{}`, "
                    "while client has version `{}`.".format(
                        server_version, client_version
                    )
                )
            client_handshake = Request(
                method="handshake/client",
                parameters=ByNameParameters({"send_confirmation": True}),
            )
            write_lsp_request(output_file, client_handshake)
            request = read_lsp_request(input_file)
            if not request.method == "handshake/socket_added":
                raise ValueError("Handshake was not successful.")
        else:
            raise ValueError("Handshake parameters from server not found.")
    else:
        raise ValueError("Handshake from server was malformed.")
