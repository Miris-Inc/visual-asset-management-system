# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A role built from a shipped permission template reaches the add-on routes its pages call.

The web Physna viewer plugin requests ``GET /addon/physna/viewer`` (``API_ADDON_PHYSNA_VIEWER``) for a
synced file, and the Miris streaming viewer requests ``GET /addon/miris/asset-status``
(``API_ADDON_MIRIS_ASSET_STATUS``) and ``POST /addon/miris/viewer-key`` (``API_ADDON_MIRIS_VIEWER_KEY``).
Each handler checks Tier 1 with ``enforceAPI`` before it reads the asset, so a role whose ``api``
constraints do not match the path receives ``403`` whatever its asset grants are. The seeded roles
reach them: the read-only seed appends the enabled add-ons' criteria to its GET and POST constraints
(``addonDefaultRoutes.ts``, spliced by ``dynamodb-authdefaults-ro-construct.ts``) and the administrator
seed matches every api path. The templates are static JSON, so they carry the criteria
unconditionally; on a deployment without the add-on they match no deployed route.

Each template is run through the import's own substitution and conversion (``substitute_variables``,
``build_constraint_data``) and then through the real policy generation and Casbin evaluation, with
only the two table reads stubbed. Reading the JSON alone would accept a criterion Casbin never
matches: a wrong operator, a misspelt value, or a constraint whose methods exclude GET.

``ADDON_ROUTE_GRANTS`` records, per add-on route, the methods each template allows. A route added to
``ADDON_ROUTES`` without an entry fails ``test_every_registered_addon_route_has_a_template_decision``,
which is where the templates that should reach it are decided.
"""

import json
import pathlib
import re
from unittest.mock import MagicMock, patch

import pytest

import backend.backend.handlers.authz as authz_module  # noqa: E402
from backend.backend.common.apiRoutes import (  # noqa: E402
    ADDON_ROUTES,
    API_ADDON_MIRIS_ASSET_STATUS,
    API_ADDON_MIRIS_VIEWER_KEY,
    API_ADDON_PHYSNA_VIEWER,
)
from backend.backend.handlers.auth import authConstraintsTemplateService as svc  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
TEMPLATES_DIR = REPO_ROOT / "documentation" / "permissionsTemplates"

ROLE_NAME = "template-role"
USER_ID = "template-user"
# What an operator supplies when importing the shipped templates.
VARIABLE_VALUES = {"ROLE_NAME": ROLE_NAME, "DATABASE_ID": "template-db", "TAG_VALUE": "locked"}
WRITE_METHODS = ("POST", "PUT", "DELETE")

# The methods each template allows on each add-on route; a template absent from a route's entry
# allows none. database-tag-admin reaches only the tag pages and deny-tagged-assets is an asset-deny
# overlay with no api constraint, so neither grants an add-on route.
ADDON_ROUTE_GRANTS = {
    API_ADDON_PHYSNA_VIEWER: {
        "database-admin.json": ("GET",),
        "database-user.json": ("GET",),
        "database-readonly.json": ("GET",),
        "global-readonly.json": ("GET",),
    },
    API_ADDON_MIRIS_ASSET_STATUS: {
        "database-admin.json": ("GET",),
        "database-user.json": ("GET",),
        "database-readonly.json": ("GET",),
        "global-readonly.json": ("GET",),
    },
    # A viewer key grants streaming only, so the read-only templates reach the route; its handler
    # still checks the asset as a read (Tier 2) before any key is returned.
    API_ADDON_MIRIS_VIEWER_KEY: {
        "database-admin.json": ("POST",),
        "database-user.json": ("POST",),
        "database-readonly.json": ("POST",),
        "global-readonly.json": ("POST",),
    },
}
ALL_METHODS = ("GET",) + WRITE_METHODS
# The operator each add-on route's criterion uses, in the templates and in the read-only seed. The
# Miris routes match exactly, so no later route under the same prefix is granted with them.
ADDON_ROUTE_OPERATORS = {
    API_ADDON_PHYSNA_VIEWER: "starts_with",
    API_ADDON_MIRIS_ASSET_STATUS: "equals",
    API_ADDON_MIRIS_VIEWER_KEY: "equals",
}
NON_ADMIN_TEMPLATES = ("database-user.json", "database-readonly.json", "global-readonly.json")


def _grant_pairs():
    """(route, template) for every template that grants a route."""
    return [(route, template) for route in sorted(ADDON_ROUTE_GRANTS, key=lambda route: route.path)
            for template in sorted(ADDON_ROUTE_GRANTS[route])]


def _template_names():
    return sorted(path.name for path in TEMPLATES_DIR.glob("*.json"))


def _stored_constraints(template_name, drop_value=None):
    """The constraints an import of the template stores, in the shape the policy builder reads.

    ``drop_value`` first removes every criterion carrying that value, which is the template as it
    reads without that grant.
    """
    document = json.loads((TEMPLATES_DIR / template_name).read_text(encoding="utf-8"))
    constraints = [
        {
            "name": constraint["name"],
            "description": constraint["description"],
            "objectType": constraint["objectType"],
            "criteriaAnd": [c for c in constraint.get("criteriaAnd", [])
                            if drop_value is None or c.get("value") != drop_value],
            "criteriaOr": [c for c in constraint.get("criteriaOr", [])
                           if drop_value is None or c.get("value") != drop_value],
            "groupPermissions": constraint.get("groupPermissions", []),
        }
        for constraint in document["constraints"]
    ]
    substituted = svc.substitute_variables(constraints, VARIABLE_VALUES)
    return [svc.build_constraint_data(constraint, ROLE_NAME, {"tokens": [USER_ID]})
            for constraint in substituted]


def _api_request(path):
    """The Tier-1 object ``enforceAPI`` evaluates for a request path."""
    return {"object__type": "api", "route__path": path}


def _concrete_path(route_path):
    """The route template with each ``{parameter}`` segment filled, as a request carries it."""
    return re.sub(r"\{[^/{}]+\}", "probe-id", route_path)


def _addon_criteria(template_name):
    """(operator, value) of every ``/addon/`` route__path criterion in the template's api constraints."""
    document = json.loads((TEMPLATES_DIR / template_name).read_text(encoding="utf-8"))
    return [
        (criterion["operator"], criterion["value"])
        for constraint in document["constraints"]
        if constraint["objectType"] == "api"
        for criterion in constraint.get("criteriaAnd", []) + constraint.get("criteriaOr", [])
        if criterion.get("field") == "route__path"
        and str(criterion.get("value", "")).startswith("/addon/")
    ]


@pytest.fixture
def template_enforcer():
    """Build the real CasbinEnforcer from a list of stored constraints.

    The per-user policy and enforcer caches are cleared on every build, so each policy is generated
    from the constraints passed in. The two table reads are the only stubs, and the denial audit
    writer is replaced so a denied check writes nothing. Both caches are restored afterwards.
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
        return_value=[{"userId": USER_ID, "roleName": ROLE_NAME}],
    ), patch.object(
        authz_module.CasbinEnforcerService, "_read_policies_batch_optimized",
        side_effect=lambda role_names: state["constraints"],
    ), patch.object(authz_module, "log_authorization", MagicMock()):
        try:
            yield build
        finally:
            authz_module.casbin_user_policy_map = previous_policy_map
            authz_module.casbin_user_enforcer_map = previous_enforcer_map


@pytest.mark.unit
class TestTemplatesGrantTheAddonRoutes:

    def test_the_templates_are_found(self):
        names = _template_names()
        assert len(names) >= 6, f"expected the shipped permission templates, found {names}"
        for route, grants in ADDON_ROUTE_GRANTS.items():
            missing = sorted(set(grants) - set(names))
            assert not missing, f"{route.path}: granting templates not found: {missing}"

    def test_every_registered_addon_route_has_a_template_decision(self):
        undecided = sorted(route.path for route in set(ADDON_ROUTES) - set(ADDON_ROUTE_GRANTS))
        assert not undecided, f"add these add-on routes to ADDON_ROUTE_GRANTS: {undecided}"

    @pytest.mark.parametrize("template_name", _template_names())
    @pytest.mark.parametrize("route", sorted(ADDON_ROUTE_GRANTS, key=lambda route: route.path),
                             ids=lambda route: route.path)
    def test_a_template_role_is_allowed_exactly_the_granted_methods(
            self, template_enforcer, route, template_name):
        expected = set(ADDON_ROUTE_GRANTS[route].get(template_name, ())) & set(route.methods)
        enforcer = template_enforcer(_stored_constraints(template_name))
        request = _api_request(_concrete_path(route.path))
        allowed = {method for method in route.methods if enforcer.enforce(request, method)}
        assert allowed == expected, (
            f"{template_name} allows {sorted(allowed)} on {route.path}, expected {sorted(expected)}")

    @pytest.mark.parametrize("template_name", NON_ADMIN_TEMPLATES)
    @pytest.mark.parametrize("route", sorted(ADDON_ROUTE_GRANTS, key=lambda route: route.path),
                             ids=lambda route: route.path)
    def test_a_non_admin_template_opens_only_the_granted_methods(
            self, template_enforcer, route, template_name):
        """Each grant sits on the template's constraint for the route's own method (GET or POST), so
        no other method reaches the path through it."""
        enforcer = template_enforcer(_stored_constraints(template_name))
        request = _api_request(route.path)
        opened = {method for method in ALL_METHODS if enforcer.enforce(request, method)}
        assert opened == set(ADDON_ROUTE_GRANTS[route][template_name]), (
            f"{template_name} allows {sorted(opened)} on {route.path}")


@pytest.mark.unit
class TestTheTemplateEvaluationCanFail:
    """Controls for the class above: an enforcer built from a template is neither allow-all nor
    deny-all, and the add-on criterion is what grants the route."""

    def test_a_template_role_is_allowed_a_granted_route_and_denied_another(self, template_enforcer):
        enforcer = template_enforcer(_stored_constraints("database-readonly.json"))
        assert enforcer.enforce(_api_request("/database/template-db/assets"), "GET") is True
        assert enforcer.enforce(_api_request("/buckets"), "GET") is False

    @pytest.mark.parametrize("route,template_name", _grant_pairs(),
                             ids=[f"{route.path}-{name}" for route, name in _grant_pairs()])
    def test_without_the_route_criterion_the_route_is_denied(self, template_enforcer, route, template_name):
        """No other prefix a template carries covers an add-on path, so the criterion is the grant
        rather than a duplicate of one."""
        enforcer = template_enforcer(_stored_constraints(template_name, drop_value=route.path))
        request = _api_request(_concrete_path(route.path))
        assert not any(enforcer.enforce(request, method) for method in route.methods)


@pytest.mark.unit
class TestTemplatesMatchTheSeededGrant:
    """The templates spell each add-on grant as the seeded read-only role does (the route's operator
    from ``ADDON_ROUTE_OPERATORS`` on the route path), so a template-built role and the seeded role
    reach the same paths. A template that grants no add-on route carries no add-on criterion."""

    def test_every_granted_route_has_an_operator(self):
        assert set(ADDON_ROUTE_OPERATORS) == set(ADDON_ROUTE_GRANTS)

    @pytest.mark.parametrize("template_name", _template_names())
    def test_the_template_spells_each_criterion_as_the_seed_does(self, template_name):
        granted = sorted((ADDON_ROUTE_OPERATORS[route], route.path)
                         for route, grants in ADDON_ROUTE_GRANTS.items() if template_name in grants)
        assert sorted(_addon_criteria(template_name)) == granted
