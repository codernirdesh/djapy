import importlib
import inspect
import json
from functools import wraps
from typing import Callable, Dict, Type, List

from django.http import HttpRequest, JsonResponse, HttpResponse
from pydantic import ValidationError
from pydantic_core import InitErrorDetails

from djapy.v2.defaults import ALLOW_METHODS_LITERAL, DEFAULT_AUTH_REQUIRED_MESSAGE, DEFAULT_METHOD_NOT_ALLOWED_MESSAGE, \
    DEFAULT_MESSAGE_ERROR
from djapy.v2.parser import extract_and_validate_request_params
from djapy.schema import Schema
import logging

__all__ = ['djapify']

from djapy.v2.response import create_json_from_validation_error, create_validation_error

MAX_HANDLER_COUNT = 1
ERROR_HANDLER_MODULE = "djapy_ext.errorhandler"


def get_required_params(view_func: Callable) -> List[inspect.Parameter]:
    """Extract required parameters from a function signature, skipping the first one."""
    signature = inspect.signature(view_func)

    required_params = []
    for index, (name, param) in enumerate(signature.parameters.items()):
        if param.kind == param.POSITIONAL_OR_KEYWORD and index != 0:
            required_params.append(param)

    return required_params


_errorhandler_functions = []
try:
    _imported_errorhandler = importlib.import_module(ERROR_HANDLER_MODULE)
    _all_handlers = dir(_imported_errorhandler)
    if len(_all_handlers) > MAX_HANDLER_COUNT:
        logging.warning(
            f"Errorhandler module should not contain more than {MAX_HANDLER_COUNT} handlers. "
            f"We discourage using more than {MAX_HANDLER_COUNT} handlers in errorhandler module.")
    for f in dir(_imported_errorhandler):
        if f.startswith('handler_'):
            _errorhandler_functions.append(getattr(_imported_errorhandler, f))
except Exception as e:
    _imported_errorhandler = None


def handle_error(request, exception):
    for _errorhandler_function in _errorhandler_functions:
        _function_signature = inspect.signature(_errorhandler_function)
        exception_param = _function_signature.parameters.get('exception')
        if exception.__class__ == exception_param.annotation:
            _data_from_error = _errorhandler_function(request, exception)
            if _data_from_error and isinstance(_data_from_error, dict):
                try:
                    return JsonResponse(_data_from_error, status=400)
                except TypeError as e:
                    logging.exception(e)
                    return JsonResponse(DEFAULT_MESSAGE_ERROR, status=500)
    return None


def djapify(view_func: Callable = None,
            login_required: bool = False,
            allowed_method: ALLOW_METHODS_LITERAL | List[ALLOW_METHODS_LITERAL] = "GET",
            openapi: bool = True,
            openapi_tags: List[str] = None) -> Callable:
    """
    :param schema_or_view_func: A pydantic model or a view function
    :param login_required: A boolean to check if the view requires login
    :param allowed_method: A string or a list of strings to check if the view allows the method
    :param openapi: A boolean to check if the view should be included in the openapi schema
    :param openapi_tags: A list of strings to tag the view in the openapi schema
    :return: A decorator that will return a JsonResponse with the schema validated data or a message
    """
    global _errorhandler_functions

    def decorator(view_func):
        required_params = get_required_params(view_func)

        @wraps(view_func)
        def _wrapped_view(request: HttpRequest, *args, **view_kwargs):
            djapy_has_login_required = getattr(_wrapped_view, 'djapy_has_login_required', False)
            djapy_allowed_method = getattr(_wrapped_view, 'djapy_allowed_method', None)

            if djapy_has_login_required and not request.user.is_authenticated:
                return JsonResponse(getattr(view_func, 'djapy_message_response', DEFAULT_AUTH_REQUIRED_MESSAGE),
                                    status=401)
            if isinstance(djapy_allowed_method, list) and request.method not in djapy_allowed_method:
                return JsonResponse(getattr(view_func, 'djapy_message_response', DEFAULT_METHOD_NOT_ALLOWED_MESSAGE),
                                    status=405)
            elif isinstance(djapy_allowed_method, str) and request.method != djapy_allowed_method:
                return JsonResponse(getattr(view_func, 'djapy_message_response', DEFAULT_METHOD_NOT_ALLOWED_MESSAGE),
                                    status=405)
            try:
                _data_kwargs = extract_and_validate_request_params(request, required_params, view_kwargs)
                response_from_view_func = view_func(request, *args, **_data_kwargs)
                if isinstance(response_from_view_func, tuple):
                    status, response = response_from_view_func
                else:
                    status, response = 200, response_from_view_func

                schema_or_type = x_schema.get(status, None)
                if inspect.isclass(schema_or_type) and issubclass(schema_or_type, Schema):
                    validated_data = schema_or_type.model_validate(response)
                    return JsonResponse(validated_data.dict(), status=status)

                if schema_or_type is not None and isinstance(response, schema_or_type) and callable(schema_or_type):
                    return JsonResponse(schema_or_type(response), status=status, safe=False)
                else:
                    raise create_validation_error("Response", "response", f"{schema_or_type.__name__}_parsing")

            except Exception as exception:
                logging.exception(exception)

                error_response = handle_error(request, exception)
                if error_response:
                    return error_response

                if isinstance(exception, ValidationError):
                    return JsonResponse(create_json_from_validation_error(exception), status=400, safe=False)

                return JsonResponse(DEFAULT_MESSAGE_ERROR, status=500)

        x_schema = view_func.__annotations__.get('return', None)

        _wrapped_view.djapy = True
        _wrapped_view.openapi = openapi
        _wrapped_view.openapi_tags = openapi_tags
        _wrapped_view.schema = x_schema
        _wrapped_view.djapy_message_response = getattr(view_func, 'djapy_message_response', {})
        _wrapped_view.required_params = required_params

        if login_required:
            setattr(_wrapped_view, 'djapy_has_login_required', True)
        if allowed_method:
            if isinstance(allowed_method, list):
                setattr(_wrapped_view, 'djapy_allowed_method', allowed_method)
            elif isinstance(allowed_method, str):
                setattr(_wrapped_view, 'djapy_allowed_method', [allowed_method])

        return _wrapped_view

    return decorator