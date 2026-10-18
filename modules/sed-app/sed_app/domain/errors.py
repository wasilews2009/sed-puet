"""Помилки застосунку з HTTP-статусом і повідомленням українською."""

from __future__ import annotations


class AppError(Exception):
    http_status = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotAuthenticated(AppError):
    http_status = 401
    code = "not_authenticated"


class Forbidden(AppError):
    http_status = 403
    code = "forbidden"


class NotFound(AppError):
    http_status = 404
    code = "not_found"


class Conflict(AppError):
    http_status = 409
    code = "conflict"


class InvalidInput(AppError):
    http_status = 422
    code = "invalid_input"


class ModuleUnavailable(AppError):
    http_status = 503
    code = "module_unavailable"
