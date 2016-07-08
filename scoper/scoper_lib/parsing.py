#!/usr/bin/env python
from __future__ import print_function

import ast
import sys
import operator
import os
import os.path
from collections import defaultdict, namedtuple, OrderedDict

import git

from auth_types import BADNESS_ORDER, STR_AUTH_MAP


def try_index(list_obj, obj):
    try:
        return list_obj.index(obj)
    except ValueError:
        return -1


def sort_routes(route_a, route_b):
    return cmp(
        try_index(BADNESS_ORDER, route_a[0]),
        try_index(BADNESS_ORDER, route_b[0])
    )


RouteResult = namedtuple("RouteResult",
                         ("route", "auth_type", "path", "rel_path",
                          "lineno", "route_lineno", "match", "route_name",
                          "commit"))


def find_decorated_funcs(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if hasattr(node, "decorator_list"):
                yield node


def get_all_routes(root_dir):
    """
    routes is the list of parsed view=foo.show() type routes we now want to map to a file and function

    for each matching route -> file::func return the parse tree for that func
    """
    files = {}

    views_dir = os.path.join(root_dir, "Uber", "uber", "views")

    routes = get_route_info(root_dir)

    # Collect all the files that contain routable functions
    for route_name in routes:
        # Lop off the view name so we just have the path
        modules_split = route_name.split(".")
        assert(len(modules_split) > 1)
        modules_split = modules_split[:-1]

        files[os.path.join(views_dir, *modules_split) + ".py"] = modules_split

    for path, modules_split in files.items():
        if not os.path.isfile(path):
            raise Exception("WTF? %s isn't a valid view path" % path)
        with file(path) as f:
            file_contents = f.read()
        tree = ast.parse(file_contents)
        for node in find_decorated_funcs(tree):
            for dec in node.decorator_list:
                full_node_name = ".".join(modules_split + [node.name])
                # Not a routable function? Don't care.
                if full_node_name not in routes:
                    continue
                auth_type = sniff_decorator_for_access(dec)
                if not auth_type:
                    continue

                rel_path = os.path.relpath(path, views_dir)
                yield RouteResult(full_node_name, auth_type, path, rel_path,
                                  node.lineno, *routes[full_node_name])
                break


def is_uberapi_decorator(dec):
    if isinstance(dec, ast.Call) and hasattr(dec, "func"):
        if isinstance(dec.func, ast.Name) and hasattr(dec.func, "id"):
            """
                Few different forms here but one looks like this:
                Call(func=Name(id='UberAPI', ctx=Load()), args=[], keywords=[
                    keyword(arg='auth', value=Call(func=Attribute(value=Name(id='api_auth', ctx=Load()), attr='service_wall_factory', ctx=Load()), args=[
                        Str(s='dispatch'),
                        Str(s='hailstorm'),
                      ], keywords=[], starargs=None, kwargs=None)),
            """
            return dec.func.id == "UberAPI" and hasattr(dec, "keywords")
    return False


def norm_auth_name(name):
    return name.replace("api_auth.", "").replace("_factory", "")


def sniff_decorator_for_access(dec):
    if not is_uberapi_decorator(dec):
        return None
    if not dec.keywords:
        # print 'some different auth deco pattern\n'
        # print astpp.dump(dec)
        """
        Call(func=Name(id='UberAPI', ctx=Load()), args=[
            Call(func=Attribute(value=Name(id='api_auth', ctx=Load()), attr='admin_or_service', ctx=Load()), args=[
                Str(s='ubill'),
                Str(s='unvaulter'),
              ], keywords=[], starargs=None, kwargs=None),
          ], keywords=[], starargs=None, kwargs=None)


        @UberAPI(auth=api_auth.admin_not_restricted_or_service(
            'lucy',
        ), use_json_dt=False)
        """

        # auth_type = dec.func.args[0].func.attr
        # TODO this doesn't really matter so we can ignore for now...
        return None

    auth_kwargs = filter(lambda x: x.arg == "auth", dec.keywords)
    if not auth_kwargs:
        # print 'some different auth deco pattern\n'
        # print astpp.dump(dec)
        return None

    auth_call = auth_kwargs[0].value
    if isinstance(auth_call, ast.Str):
        return STR_AUTH_MAP.get(auth_call.s, auth_call.s)

    elif isinstance(auth_call, ast.Call):
        # print astpp.dump(dec.keywords[0])
        # print 'got a call..........'
        # print astpp.dump(dec.keywords[0])

        if hasattr(auth_call, "func"):
            if all(hasattr(auth_call.func, x) for x in {"value", "attr"}):
                if not auth_call.func.value.id == "api_auth":
                    print('something else weird is wrong...............', file=sys.stderr)
                    return "unknown"
                return norm_auth_name(auth_call.func.attr)
            if hasattr(auth_call.func, "id"):
                return norm_auth_name(auth_call.func.id)

    elif isinstance(auth_call, (ast.Attribute, ast.Name, ast.Call)):
        # custom auth wall? IDKLOL.
        # @UberAPI(auth=_zendesk_user_wall)
        # def index(request):
        return norm_auth_name(get_fully_qualified_func_name(auth_call))
    # return "unknown" for crap like:
    # @UberAPI(auth=api_auth.user_wall_factory(
    #    'payment_profile',
    #    object_getter=_pp_object_getter(rollout_setting_name='pp_deposit_request'))
    #    if not config.get('money.payment.airtel_money.skip_check_bonus_auth', True)
    #    else "no_auth_required")
    # It's hard to determine statically which auth wall it'd use.
    return "unknown"


def get_fully_qualified_func_name(v):
    name_parts = []
    while hasattr(v, "attr"):
        name_parts.append(v.attr)
        v = v.value
    name_parts.append(v.id)
    return ".".join(reversed(name_parts))


class RouteVisitor(ast.NodeVisitor):
    def __init__(self):
        self.routable_funcs = {}

    @staticmethod
    def is_add_route_call(call):
        if hasattr(call, "func"):
            func = call.func
            if hasattr(func, "id"):
                return func.id == "add_route"
        return False

    def visit_Expr(self, node):
        if not hasattr(node, 'value'):
            return
        if not isinstance(node.value, ast.Call):
            return
        call = node.value
        if not self.is_add_route_call(call):
            return

        route_name = call.args[0].s
        route_match = call.args[1].s

        for kw in call.keywords:
            k = kw.arg
            v = kw.value
            if k == "view":
                if hasattr(v, "value"):
                    full_name = get_fully_qualified_func_name(v)
                    if "." not in full_name:
                        # Crap, someone imported the view into the local
                        # namespace so we don't know the fully qualified
                        # name. Why would you do that?
                        return
                    self.routable_funcs[full_name] = (v.lineno, route_match, route_name)
        self.generic_visit(node)


def get_route_info(root_path):
    routing_path = os.path.join(root_path, "Uber", "uber", "routing.py")
    with open(routing_path) as f:
        file_contents = f.read()
    tree = ast.parse(file_contents)
    visitor = RouteVisitor()
    visitor.visit(tree)
    all_routes = visitor.routable_funcs

    # Get the commit that added each route and tack it onto the details
    route_linenos = sorted(set(x[0] for x in all_routes.values()))
    repo = git.Repo(root_path)
    blame_by_line = get_line_blames(repo, routing_path, route_linenos)
    for name, details in all_routes.iteritems():
        all_routes[name] = details + (blame_by_line[details[0]],)
    return all_routes


def get_routes_by_auth_type(all_routes):
    # for different types see lib/api_auth.py
    routes_by_auth_type = defaultdict(list)

    for route in all_routes:
        routes_by_auth_type[route.auth_type].append(
            route
        )
    routes_by_auth_type = OrderedDict(
        sorted(routes_by_auth_type.items(), cmp=sort_routes)
    )
    for k, v in routes_by_auth_type.iteritems():
        v.sort(key=operator.attrgetter("route"))
    return routes_by_auth_type


def get_line_blames(repo, filename, linenos):
    tlc = 0
    line_blames = {}
    for commit, lines in repo.blame('HEAD', filename):
        # 1-indexed to 0-indexed
        these_lines = set()
        for lineno in linenos:
            if tlc <= (lineno - 1) < (tlc + len(lines)):
                these_lines.add(lineno)
        for lineno in these_lines:
            line_blames[lineno] = commit
        tlc += len(lines)
    return line_blames