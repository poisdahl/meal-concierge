"""Read a saved planning menu and build an Oda merchandise review."""
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import hosted_oda
import install
import menu_planning as mp
from on_demand import MAX_MENU, _private_directory, _read_file, _json
from planner import saved_menu_minimum_evaluation
from product_observations import _oda_ore, parse_package
from product_planner import build_product_plan, ingredient_search, menu_requirements
from runtime_ownership import file_lock
from shopping_lists import build_list, effective_ingredient_decisions


MAX_REQUIREMENTS = 8
MAX_PACKAGES = 24
MAX_BASELINE_LINES = 32


@contextmanager
def source(home, workspace):
    if not isinstance(home, str):
        raise ValueError("provide the original store-free planning home")
    directory = Path(home)
    if (not directory.is_absolute() or str(directory) != home or directory == workspace
            or not directory.is_relative_to(workspace) or directory.resolve(strict=True) != directory):
        raise ValueError("use the original planning home in this Dot workspace")
    _private_directory(directory)
    with file_lock(directory / ".installer.lock"):
        meta = install.completed_manifest(directory)
        try:
            root = Path(meta["code_root"])
            release = Path(meta["release"])
            paths = {key: Path(meta["paths"][key]) for key in ("state", "config")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("the original planning installation manifest is invalid") from exc
        if (meta.get("manager") != "external" or meta.get("home") != home
                or not root.is_absolute() or root.resolve(strict=True) != root or not root.is_relative_to(workspace)
                or read_private_json(root / "owner.json", 16384).get("home") != home
                or (root / "current").resolve(strict=True) != release.resolve(strict=True)
                or any(not path.is_relative_to(directory) or path.resolve(strict=True) != path for path in paths.values())):
            raise ValueError("the original planning installation or ownership changed")
        settings = read_private_json(paths["config"], 16384)
        if settings.get("provider") != "none":
            raise ValueError("select the original store-free planning household")
        with install.data_ownership(meta):
            _private_directory(paths["state"])
            state = read_private_json(paths["state"] / "state.json", MAX_MENU)
            if state.get("provider") != "none":
                raise ValueError("select the original store-free planning household")
            yield menu_source(state, home)


def read_private_json(path, maximum):
    if path.lstat().st_mode & 0o077:
        raise ValueError("the original planning household files must be private")
    value = _json(_read_file(path, maximum))
    if not isinstance(value, dict):
        raise ValueError("the original planning household file must contain a JSON object")
    return value


def menu_source(state, home):
    menu = state.get("menu")
    if not menu:
        raise ValueError("save a menu before preparing Oda shopping")
    current = build_list(state)
    profile = state.get("profile") or {}
    minimums = saved_menu_minimum_evaluation(menu, profile)
    if (minimums.get("enforced_status", minimums.get("status")) != "pass"
            and (minimums.get("complete_menu") or menu.get("weekly_plan_complete") is True)):
        raise ValueError("the saved weekly menu must satisfy its dietary minimums")
    stock = current["stock_check"]
    decisions, _ = effective_ingredient_decisions(state, menu)
    shopping = mp.shopping_menu(menu)
    requirements, unresolved = menu_requirements(shopping, ingredient_decisions=decisions)
    if unresolved or not 1 <= len(requirements) <= MAX_REQUIREMENTS:
        raise ValueError("Oda saved-menu shopping requires one to eight measured requirements and no unresolved rows")
    binding = {"home": home, "menu_ref": current["menu_ref"], "list_ref": current["list_ref"],
               "stock_ref": stock["stock_ref"], "context_sha256": mp.digest({"shopping": shopping, "profile": profile})}
    unanswered = [{key: row[key] for key in ("requirement_id", "item", "unit", "status", "pantry",
                                          "accepted_pantry_assumption")}
                  | {"remaining_quantity": row.get("remaining_quantity")}
                  for row in stock["items"] if row["status"] in {"unanswered", "partial"}]
    return {"binding": binding, "requirements": requirements, "menu": shopping, "profile": profile,
            "decisions": decisions, "unanswered_stock": unanswered}


def search_arguments(context):
    queries = dict.fromkeys(ingredient_search(row["item"], "oda") for row in context["requirements"])
    return {"queries": list(queries), "page": 1, "size": 5}


def candidates(context, views):
    rows = []
    for row in context["requirements"]:
        view = views[ingredient_search(row["item"], "oda")]
        rows.append({"requirement_id": row["requirement_id"], "ingredient": row["item"],
                     "required": row["quantity"], "unit": row["unit"],
                     "products": [{key: product[key] for key in ("product_id", "name", "availability", "display")}
                                  for product in view["products"]]})
    return rows


def quote(context, views, choices):
    binding = context["binding"]
    observed = {row["requirement_id"]: views[ingredient_search(row["item"], "oda")]
                for row in context["requirements"]}
    if (not isinstance(choices, dict) or choices.keys() != observed.keys()
            or any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in choices.values())
            or len(set(choices.values())) != len(choices)):
        raise ValueError("select one distinct observed Oda product for each measured requirement")
    plan = build_product_plan(
        provider="oda", binding={key: value for key, value in binding.items() if key != "home"},
        menu=context["menu"], observations=observed,
        candidate_approvals=[{"requirement_id": ref, "candidate_refs": [product_id]}
                             for ref, product_id in choices.items()],
        dietary_profile=context["profile"], ingredient_decisions=context["decisions"], price_mode="estimate",
    )
    if plan["status"] != "prepared":
        reasons = sorted({issue["reason"] for issue in plan["unresolved_requirements"]})
        raise ValueError("Oda could not prepare exactly covering packages: " + ", ".join(reasons))
    rows = []
    for requirement in plan["requirements"]:
        selection = requirement["selection"]
        product, = selection["products"]
        identifier = requirement["requirement_id"]
        chosen = next(row for row in observed[identifier]["products"] if row["product_id"] == choices[identifier])
        option, = chosen["purchase_options"]
        quantity = product["quantity"]
        merchandise = product["merchandise_ore"]
        unit_price = option.get("merchandise_ore")
        if (selection.get("coverage_status", "exact") != "exact" or not selection.get("coverage")
                or chosen.get("package", {}).get("quantity_kind") in {"minimum", "expected"}
                or option["price_kind"] != "exact" or type(unit_price) is not int
                or type(merchandise) is not int or merchandise != quantity * unit_price):
            raise ValueError("Oda requires fixed package coverage and an observed regular merchandise price")
        rows.append({"requirement_id": identifier, "ingredient": requirement["item"],
                     "required": requirement["quantity"], "unit": requirement["unit"],
                     "coverage": selection["coverage"], "product_id": product["product_ref"],
                     "name": product["name"], "package": chosen["package"],
                     "package_count": quantity, "package_price_ore": unit_price, "merchandise_ore": merchandise,
                     "dietary_conditions": dict(Counter(item["condition"]
                                                        for item in product.get("dietary_assessments", [])))})
    package_count = sum(row["package_count"] for row in rows)
    if not 1 <= package_count <= MAX_PACKAGES:
        raise ValueError("Oda saved-menu shopping can apply up to 24 packages in one cart operation")
    return {"currency": "NOK", "items": rows, "package_count": package_count,
            "merchandise_ore": sum(row["merchandise_ore"] for row in rows),
            "unanswered_stock_rows": len(context["unanswered_stock"]), "unanswered_stock": context["unanswered_stock"]}


def ore(value, *, signed=False):
    return int(Decimal(hosted_oda.money(value, signed=signed)) * 100)


def cart_amounts(cart):
    return {key: ore(cart["totalBreakdown"][key], signed=key in {"productDiscounts", "couponDiscount"})
            for key in sorted(hosted_oda.BREAKDOWN_FIELDS)}


def cart_snapshot(cart):
    lines = {}
    for group in cart["groups"]:
        for line in group["items"]:
            product = line["product"]
            identity = str(product["id"])
            lines[identity] = {"name": product["name"], "quantity": int(line["quantity"]),
                               "package": parse_package(product.get("description"), provider="oda"),
                               "package_price_ore": _oda_ore(product.get("price")),
                               "details_sha256": hosted_oda.arguments_digest(
                                   {key: product.get(key) for key in ("description", "price")}),
                               "amount_ore": ore(line["totalGrossAmount"])}
    return {"lines": lines, "amounts_ore": cart_amounts(cart)}


def quoted_cart_lines(quote, baseline):
    lines = dict(baseline["lines"]) if baseline else {}
    for row in quote["items"]:
        identity = str(row["product_id"])
        original = lines.get(identity, {})
        quantity = max(row["package_count"], original.get("quantity", 0))
        price = row["package_price_ore"]
        lines[identity] = {**original, "name": row["name"], "quantity": quantity, "package": row["package"],
                           "package_price_ore": price, "amount_ore": quantity * price}
    return lines


def quote_with_baseline(quote, baseline):
    existing = baseline["lines"]
    reviewed = quoted_cart_lines(quote, baseline)
    additions = []
    for row in quote["items"]:
        identity = str(row["product_id"])
        count = existing.get(identity, {}).get("quantity", 0)
        if count:
            expected = {**reviewed[identity], "quantity": count, "amount_ore": count * row["package_price_ore"]}
            if any(existing[identity][key] != value for key, value in expected.items()):
                raise ValueError("an existing menu product's package or price differs from Oda search")
        additional = reviewed[identity]["quantity"] - count
        additions.append({**row, "existing_package_count": count, "additional_package_count": additional,
                          "additional_merchandise_ore": additional * row["package_price_ore"]})
    return {**quote, "items": additions,
            "existing_items": [{"product_id": int(identity), "name": line["name"], "quantity": line["quantity"],
                                "merchandise_ore": line["amount_ore"]} for identity, line in sorted(existing.items())],
            "existing_cart_amounts_ore": baseline["amounts_ore"],
            "additional_package_count": sum(row["additional_package_count"] for row in additions),
            "additional_merchandise_ore": sum(row["additional_merchandise_ore"] for row in additions),
            "resulting_merchandise_ore": sum(row["amount_ore"] for row in reviewed.values())}


def cart_prices(cart, quote, baseline=None):
    snapshot = cart_snapshot(cart)
    expected, actual = quoted_cart_lines(quote, baseline), snapshot["lines"]
    observed_amounts = {key: row["amount_ore"] for key, row in actual.items()}
    breakdown = snapshot["amounts_ore"]
    line_fields = {"quantity", "amount_ore"}
    product_details_match = (actual.keys() == expected.keys()
                             and all(all(actual[identity][key] == row[key] for key in row.keys() - line_fields)
                                     for identity, row in expected.items()))
    quantities_match = ({key: row["quantity"] for key, row in actual.items()}
                        == {key: row["quantity"] for key, row in expected.items()})
    return {"currency": "NOK", "line_amounts_ore": observed_amounts, "breakdown_ore": breakdown,
            "product_details_match": product_details_match, "quantities_match": quantities_match,
            "matches_review": (product_details_match and quantities_match
                               and observed_amounts == {key: row["amount_ore"] for key, row in expected.items()}
                               and breakdown["subtotal"] == sum(row["amount_ore"] for row in expected.values()))}
