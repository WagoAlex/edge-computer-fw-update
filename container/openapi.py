#!/usr/bin/env python3
"""The OpenAPI 3.1 document for what THIS build implements.

Generated from the provider registry at request time, so the spec cannot drift
from the code: add a provider, and the parameter shows up here with the dataType
and path the cassette gives it.

It is deliberately not a copy of WAGO's 40-path document. This is a strict subset
- five paths, no discovery collections - and `info.description` says so, so a
generated client fails at build time on the parts we do not have rather than at
runtime against a device.
"""
import providers
from providers import firmwareupdate as fw
from providers import meta

_OK = {"description": "Success",
       "content": {"application/vnd.api+json": {"schema": {"$ref": "#/components/schemas/document"}}}}
_ERR = {"description": "Error",
        "content": {"application/vnd.api+json": {"schema": {"$ref": "#/components/schemas/errors"}}}}


def _param_ids():
    """Every id this build can serve, with its type metadata. Dynamic instance
    ids (routes, extra ports, users) are shown by example, not enumerated -
    their count depends on the device at the moment of the call."""
    out = {}
    for pid in sorted(providers.PARAMS):
        v = providers.param_value(pid)
        out[pid] = meta.describe(pid, v)
    return out


def document(order_number, firmware_version, wda_version):
    params = _param_ids()
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "WAGO Device Access: REST-API (rauc-backed re-implementation)",
            "version": wda_version,
            "summary": f"WDA-compatible surface for the WAGO Edge Computer "
                       f"({order_number}, firmware {firmware_version}).",
            "description": (
                "A STRICT SUBSET of WAGO's WDA REST-API. Envelopes, parameter ids "
                "and method semantics match WDA 1.5.2, but only the paths below "
                "are implemented: there are no discovery collections "
                "(/wda/parameters, /wda/methods, /wda/*-definitions, "
                "/wda/features, /wda/monitoring-lists, /wda/devices), no OAuth2 "
                "or token auth, and no PATCH of parameters - every parameter here "
                "is read-only apart from the firmware-update state machine.\n\n"
                "The backend is stock RAUC over the host D-Bus plus the kernel's "
                "own /sys and /proc views, not WAGO's wdx provider. Bundles are "
                "self-signed and are NOT accepted by a genuine PFC/TP600 WDA.\n\n"
                "Unlike a real device, this document requires authentication."),
            "license": {"name": "Proprietary - WAGO internal"},
        },
        "servers": [{"url": "/", "description": "This server."}],
        "security": [{"password_based": []}],
        "components": {
            "securitySchemes": {"password_based": {"type": "http", "scheme": "basic"}},
            "schemas": {
                "document": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "object"},
                        "jsonapi": {"type": "object",
                                    "properties": {"version": {"type": "string"}}},
                        "links": {"type": "object"},
                        "meta": {"type": "object"}},
                    "required": ["data"]},
                "errors": {
                    "type": "object",
                    "properties": {"errors": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"status": {"type": "string"},
                                       "detail": {"type": "string"}}}}},
                    "required": ["errors"]},
            },
        },
        "paths": {
            "/wda": {"get": {
                "operationId": "getServiceRoot", "summary": "Device identity",
                "responses": {"200": _OK, "401": _ERR}}},
            "/wda/parameters/{parameter_id}": {
                "parameters": [{"name": "parameter_id", "in": "path", "required": True,
                                "schema": {"type": "string", "enum": sorted(params)},
                                "description":
                                    "Fixed ids are enumerated. Instance ids also "
                                    "resolve and are not listed, because their count "
                                    "is whatever the device has right now: "
                                    "0-0-networking-ethernetports-<n>-*, "
                                    "-bridges-<n>-*, -routing-currentroutes-<n>-*, "
                                    "0-0-localusers-<uid>-*."}],
                "get": {"operationId": "getParameter",
                        "summary": "Read one parameter",
                        "responses": {"200": _OK, "401": _ERR, "404": _ERR}}},
            "/wda/parameter-definitions/{parameter_definition_id}/enum": {
                "parameters": [{"name": "parameter_definition_id", "in": "path",
                                "required": True,
                                "schema": {"type": "string",
                                           "enum": sorted(providers.ENUMS)}}],
                "get": {"operationId": "getParameterEnum",
                        "summary": "Enum members of a parameter",
                        "responses": {"200": _OK, "401": _ERR, "404": _ERR}}},
            "/wda/methods/{method_id}/runs": {
                "parameters": [{"name": "method_id", "in": "path", "required": True,
                                "schema": {"type": "string",
                                           "enum": sorted(providers.METHODS)}},
                               {"name": "result-behavior", "in": "query",
                                "schema": {"type": "string", "enum": ["sync"]}}],
                "post": {
                    "operationId": "runMethod", "summary": "Invoke a method",
                    "requestBody": {"content": {"application/vnd.api+json": {"schema": {
                        "type": "object", "properties": {"data": {"type": "object",
                            "properties": {"type": {"const": "runs"},
                                           "attributes": {"type": "object",
                                               "properties": {"inArgs": {"type": "object"}}}}}}}}}},
                    "responses": {
                        "201": {"description":
                                "Run created. A method that could not be invoked "
                                "also returns 201, with attributes.executionStatus "
                                "\"error\", code \"26\" and a "
                                "domainSpecificStatusCode - 95 not activated, "
                                "90 already active.",
                                "content": {"application/vnd.api+json": {
                                    "schema": {"$ref": "#/components/schemas/document"}}}},
                        "401": _ERR, "404": _ERR}}},
            "/files/{file_id}": {
                "parameters": [{"name": "file_id", "in": "path", "required": True,
                                "schema": {"type": "string", "pattern": "^[0-9a-f]+$"},
                                "description": "An id from 0-0-firmwareupdate-getuploadids."}],
                "patch": {
                    "operationId": "uploadChunk",
                    "summary": "Upload one chunk of a firmware bundle",
                    "requestBody": {"required": True, "content": {
                        "multipart/byteranges": {"schema": {"type": "string",
                                                            "format": "binary"}}}},
                    "responses": {"204": {"description": "Chunk written"},
                                  "400": _ERR, "401": _ERR, "404": _ERR}}},
            "/health": {"get": {
                "operationId": "getHealth",
                "summary": "Container liveness. Not a WDA path and not authenticated.",
                "security": [],
                "responses": {"200": {"description": "Alive", "content": {
                    "application/json": {"schema": {"type": "object", "properties": {
                        "status": {"const": "ok"}}}}}}}}},
        },
        "x-parameter-metadata": params,
        "x-firmware-update-enums": {
            "status": fw.STATUS_NAMES, "errorcause": fw.ERROR_CAUSES},
    }
