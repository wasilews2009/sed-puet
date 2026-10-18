"""Помилки предметної області. Кожна знає свій HTTP-статус і машинний код для клієнта."""

from __future__ import annotations

from typing import Any


class RoutingError(Exception):
    http_status = 400
    code = "routing_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(RoutingError):
    http_status = 404
    code = "not_found"


class AccessDenied(RoutingError):
    http_status = 403
    code = "access_denied"


class InvalidState(RoutingError):
    """Дія неможлива в поточному стані маршруту/кроку/документа."""

    http_status = 409
    code = "invalid_state"


class TemplateNotSelected(RoutingError):
    """Для виду документа немає шаблону або їх кілька — потрібно вказати template_code."""

    http_status = 409
    code = "template_not_selected"


class AssigneeUnresolved(RoutingError):
    """Посаду не вдалося однозначно зіставити з людиною (вакансія або кілька кандидатів)."""

    http_status = 409
    code = "assignee_unresolved"


class BlockingFindings(RoutingError):
    """На версію є відкриті критичні зауваження комплаєнс-аудиту."""

    http_status = 409
    code = "blocking_compliance_findings"


class SignatureRejected(RoutingError):
    """Контейнер КЕП не пройшов перевірку; рішення по кроку не прийнято."""

    http_status = 422
    code = "signature_rejected"


class TemplateConfigError(RoutingError):
    http_status = 500
    code = "template_config_error"
