# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read-only template roles reach POST on the Miris viewer key route and no other add-on POST.

The Miris streaming viewer gets its key with ``POST /addon/miris/viewer-key``
(``API_ADDON_MIRIS_VIEWER_KEY``), whose handler checks Tier 1 with ``enforceAPI`` on the request path.
The read-only templates (``database-readonly``, ``global-readonly``) carry one ``equals`` criterion
for it in their ``{{ROLE_NAME}}-api-routes-post`` constraint, the OR list of non-mutating POST
routes, which grants POST only; the status route's GET criterion is ``equals`` too. An exact match
keeps a later route under either prefix out of the read-only grant. Each property is checked at the
Casbin level, through the real policy generation and evaluation, with only the table reads stubbed.
"""

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

import backend.backend.handlers.authz as authz_module  # noqa: E402
from backend.backend.common.apiRoutes import (  # noqa: E402
    API_ADDON_MIRIS_ASSET_STATUS,
    API_ADDON_MIRIS_VIEWER_KEY,
)
from backend.backend.handlers.auth import authConstraintsTemplateService as svc  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
TEMPLATES_DIR = REPO_ROOT / "documentation" / "permissionsTemplates"

READONLY_TEMPLATES = ("database-readonly.json", "global-readonly.json")
DATABASE_ID = "db1"
ROLE_NAME = "template-role"
USER_ID = "readonly-user"
VARIABLE_VALUES = {"ROLE_NAME": ROLE_NAME, "DATABASE_ID": DATABASE_ID}
POST_CONSTRAINT_NAME = f"{ROLE_NAME}-api-routes-post"

VIEWER_KEY_PATH = API_ADDON_MIRIS_VIEWER_KEY.path
STATUS_PATH = API_ADDON_MIRIS_ASSET_STATUS.path
DENIED_POST_PATHS = (
    f"/database/{DATABASE_ID}/assets/a1",
    STATUS_PATH,
    "/addon/physna/viewer",
    "/addon/miris",
    "/addon/miris/viewer",
    "/addon/physna/viewer-key",
    f"{VIEWER_KEY_PATH}/extra",
    f"{VIEWER_KEY_PATH}s",
    f"{VIEWER_KEY_PATH}s-admin",
)
DENIED_GET_PATHS = (
    f"{STATUS_PATH}-purge",
    f"{STATUS_PATH}/x",
    f"{STATUS_PATH}es",
    "/addon/miris",
    "/addon/miris/asset",
)


def _template_constraints(template_name):
    """The constraints an import of the template stores, in the shape the policy builder reads."""
    document = json.loads((TEMPLATES_DIR / template_name).read_text(encoding="utf-8"))
    constraints = [
        {
            "name": constraint["name"],
            "description": constraint["description"],
            "objectType": constraint["objectType"],
            "criteriaAnd": constraint.get("criteriaAnd", []),
            "criteriaOr": constraint.get("criteriaOr", []),
            "groupPermissions": constraint.get("groupPermissions", []),
        }
        for constraint in document["constraints"]
    ]
    substituted = svc.substitute_variables(constraints, VARIABLE_VALUES)
    return [svc.build_constraint_data(constraint, ROLE_NAME, {"tokens": [USER_ID]})
            for constraint in substituted]


@pytest.fixture
def role_enforcer():
    """Build the real CasbinEnforcer for one role from a list of stored constraints.

    Only the two table reads are stubbed and the denial audit writer is replaced. The per-user
    caches are cleared on every build and restored afterwards.
    """
    state = {"constraints": []}
    previous_policy_map = authz_module.casbin_user_policy_map
    previous_enforcer_map = authz_module.casbin_user_enforcer_map

    def build(constraints):
        state["constraints"] = constraints
        authz_module.casbin_user_policy_map = {}
        authz_module.casbin_user_enforcer_map = {}
        return authz_module.CasbinEnforcer(
            {"tokens": [USER_ID], "roles": [ROLE_NAME], "mfaEnabled": True})

    with patch.object(
        authz_module.CasbinEnforcerService, "_read_current_user_roles_from_table",
        side_effect=lambda: [{"userId": USER_ID, "roleName": ROLE_NAME}],
    ), patch.object(
        authz_module.CasbinEnforcerService, "_read_policies_batch_optimized",
        side_effect=lambda role_names: state["constraints"],
    ), patch.object(authz_module, "log_authorization", MagicMock()):
        try:
            yield build
        finally:
            authz_module.casbin_user_policy_map = previous_policy_map
            authz_module.casbin_user_enforcer_map = previous_enforcer_map


def _api(path):
    """The Tier-1 object ``enforceAPI`` evaluates for a request path."""
    return {"object__type": "api", "route__path": path}


def _carries_viewer_key(constraint):
    return any(criterion.get("value") == VIEWER_KEY_PATH
               for criterion in constraint.get("criteriaAnd", []) + constraint.get("criteriaOr", []))


def _without_viewer_key_grant(constraints):
    """The constraints with the viewer key criterion removed from whichever constraint holds it."""
    stripped = []
    for constraint in constraints:
        constraint = dict(constraint)
        for key in ("criteriaAnd", "criteriaOr"):
            constraint[key] = [c for c in constraint.get(key, []) if c.get("value") != VIEWER_KEY_PATH]
        stripped.append(constraint)
    return stripped


@pytest.mark.unit
class TestReadOnlyRolesGetViewerKeys:

    def test_the_paths_are_the_registered_routes(self):
        assert API_ADDON_MIRIS_VIEWER_KEY.matches(VIEWER_KEY_PATH)
        assert API_ADDON_MIRIS_ASSET_STATUS.matches(STATUS_PATH)
        assert API_ADDON_MIRIS_VIEWER_KEY.methods == ("POST",)
        assert API_ADDON_MIRIS_ASSET_STATUS.methods == ("GET",)

    @pytest.mark.parametrize("template_name", READONLY_TEMPLATES)
    def test_post_on_the_viewer_key_route_is_allowed(self, role_enforcer, template_name):
        enforcer = role_enforcer(_template_constraints(template_name))
        assert enforcer.enforce(_api(VIEWER_KEY_PATH), "POST") is True

    @pytest.mark.parametrize("path", DENIED_POST_PATHS)
    @pytest.mark.parametrize("template_name", READONLY_TEMPLATES)
    def test_post_elsewhere_is_denied(self, role_enforcer, template_name, path):
        enforcer = role_enforcer(_template_constraints(template_name))
        assert enforcer.enforce(_api(path), "POST") is False

    @pytest.mark.parametrize("path", DENIED_GET_PATHS)
    @pytest.mark.parametrize("template_name", READONLY_TEMPLATES)
    def test_get_beside_the_status_route_is_denied(self, role_enforcer, template_name, path):
        enforcer = role_enforcer(_template_constraints(template_name))
        assert enforcer.enforce(_api(path), "GET") is False

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    @pytest.mark.parametrize("template_name", READONLY_TEMPLATES)
    def test_no_other_method_reaches_the_viewer_key_route(self, role_enforcer, template_name, method):
        enforcer = role_enforcer(_template_constraints(template_name))
        assert enforcer.enforce(_api(VIEWER_KEY_PATH), method) is False

    @pytest.mark.parametrize("template_name", READONLY_TEMPLATES)
    def test_get_on_the_status_route_is_allowed(self, role_enforcer, template_name):
        enforcer = role_enforcer(_template_constraints(template_name))
        assert enforcer.enforce(_api(STATUS_PATH), "GET") is True


@pytest.mark.unit
class TestTheGrantIsOneCriterionInThePostConstraint:
    """Controls: the criterion is what grants the route, and it sits in the POST-only OR list."""

    @pytest.mark.parametrize("template_name", READONLY_TEMPLATES)
    def test_without_the_criterion_the_route_is_denied(self, role_enforcer, template_name):
        enforcer = role_enforcer(_without_viewer_key_grant(_template_constraints(template_name)))
        assert enforcer.enforce(_api(VIEWER_KEY_PATH), "POST") is False
        # The rest of the role still works, so the denial is not an empty policy.
        assert enforcer.enforce(_api(STATUS_PATH), "GET") is True
        assert enforcer.enforce(_api("/search"), "POST") is True

    @pytest.mark.parametrize("template_name", READONLY_TEMPLATES)
    def test_the_grant_is_one_equals_criterion_of_the_post_constraint(self, template_name):
        grants = [c for c in _template_constraints(template_name) if _carries_viewer_key(c)]
        assert len(grants) == 1
        grant = grants[0]
        assert grant["name"] == POST_CONSTRAINT_NAME
        assert grant["objectType"] == "api"
        assert grant["criteriaAnd"] in ([], None)
        criteria = [(c["field"], c["operator"]) for c in grant["criteriaOr"] if c["value"] == VIEWER_KEY_PATH]
        assert criteria == [("route__path", "equals")]
        assert [(p["permission"], p["permissionType"]) for p in grant["groupPermissions"]] == [
            ("POST", "allow")]
