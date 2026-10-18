"""Чисті тести політики доступу і нормалізації реквізитів (без БД)."""

from __future__ import annotations

import uuid

import pytest

from sed_app.domain.access import (AccessScope, DocumentFacts, OrgTree, can_review, can_start_route, can_view)
from sed_app.domain.errors import InvalidInput
from sed_app.domain.requisites import normalize_requisite
from sed_db.enums import AccessLevel, DocumentStatus

ROOT, INST, DEPT, OTHER = (uuid.uuid4() for _ in range(4))
TREE = OrgTree(parents={ROOT: None, INST: ROOT, DEPT: INST, OTHER: ROOT})
AUTHOR, MEMBER, HEAD, STRANGER, SIGNER = (uuid.uuid4() for _ in range(5))


def scope(user: uuid.UUID, *, managed: frozenset[uuid.UUID] | set[uuid.UUID] = frozenset(),
          member: frozenset[uuid.UUID] | set[uuid.UUID] = frozenset(),
          wide: bool = False) -> AccessScope:
    return AccessScope(user_id=user, all_documents=wide, managed_units=frozenset(managed),
                       member_units=frozenset(member))


def doc(level: AccessLevel = AccessLevel.INTERNAL, status: DocumentStatus = DocumentStatus.DRAFT) -> DocumentFacts:
    return DocumentFacts(id=uuid.uuid4(), author_id=AUTHOR, org_unit_id=DEPT, access_level=level, status=status,
                         route_participant_ids=frozenset({SIGNER}))


def test_subtree_includes_descendants_only() -> None:
    assert TREE.subtree([INST]) == {INST, DEPT}
    assert TREE.subtree([ROOT]) == {ROOT, INST, DEPT, OTHER}


@pytest.mark.parametrize("level", list(AccessLevel))
def test_view_matrix(level: AccessLevel) -> None:
    d = doc(level)
    assert can_view(scope(AUTHOR), d)
    assert can_view(scope(SIGNER), d)                                 # учасник маршруту
    assert can_view(scope(HEAD, managed=TREE.subtree([INST])), d)     # директор інституту бачить кафедру
    assert can_view(scope(STRANGER, wide=True), d)                    # ректор
    assert can_view(scope(MEMBER, member={DEPT}), d) == (level in (AccessLevel.PUBLIC, AccessLevel.INTERNAL))
    assert can_view(scope(STRANGER, member={OTHER}), d) == (level is AccessLevel.PUBLIC)


def test_route_participant_can_view_but_not_review() -> None:
    d = doc(AccessLevel.PERSONAL_DATA)
    assert can_view(scope(SIGNER), d) and not can_review(scope(SIGNER), d)


def test_review_closed_in_final_status() -> None:
    finals = [s for s in DocumentStatus if not can_review(scope(AUTHOR), doc(status=s))]
    assert finals, "мають бути фінальні статуси, у яких правки заборонені"
    assert can_review(scope(AUTHOR), doc(status=DocumentStatus.DRAFT))


def test_start_route_only_author_manager_or_wide() -> None:
    d = doc()
    assert can_start_route(scope(AUTHOR), d)
    assert can_start_route(scope(HEAD, managed={DEPT}), d)
    assert not can_start_route(scope(MEMBER, member={DEPT}), d)
    assert not can_start_route(scope(AUTHOR), doc(status=DocumentStatus.ON_APPROVAL))


def test_normalize_requisites() -> None:
    assert normalize_requisite("document_date", " 2026-10-17 ") == "2026-10-17"
    assert normalize_requisite("title", "  Про   затвердження\nположення ") == "Про затвердження положення"
    assert normalize_requisite("author", "Ірина Савчук") == {"full_name": "Ірина Савчук", "position": None,
                                                             "org_unit": None}
    parties = normalize_requisite("interested_parties", [{"full_name": "А Б", "role": "виконавець"}])
    assert parties[0]["role"] == "виконавець"
    assert len(normalize_requisite("registration_number", "x" * 500)) == 64


@pytest.mark.parametrize(("key", "value"), [
    ("document_date", "17.10.2026"), ("author", {"full_name": ""}), ("author", {"full_name": "А", "evil": "x"}),
    ("interested_parties", "не список"), ("title", "   "), ("unknown", "x"), ("summary", 5),
])
def test_normalize_rejects(key: str, value: object) -> None:
    with pytest.raises(InvalidInput):
        normalize_requisite(key, value)
