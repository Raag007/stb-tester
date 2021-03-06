import os
import sys
import traceback
from collections import namedtuple
from contextlib import contextmanager

import stbt
from _stbt.state_watch import new_state_sender


def _save_screenshot(dut, exception, save_jpg, save_png):
    import cv2

    if not save_jpg and not save_png:
        return

    screenshot = getattr(exception, "screenshot", None)
    if screenshot is None and dut._display:  # pylint: disable=protected-access
        screenshot = dut._display.last_used_frame  # pylint: disable=protected-access
    if screenshot is None:
        screenshot = dut.get_frame()  # pylint: disable=protected-access

    if save_png:
        cv2.imwrite("screenshot.png", screenshot)
        sys.stderr.write("Saved screenshot to 'screenshot.png'.\n")

    if save_jpg:
        cv2.imwrite(
            'thumbnail.jpg',
            cv2.resize(screenshot, (
                640, 640 * screenshot.shape[0] // screenshot.shape[1])),
            [cv2.IMWRITE_JPEG_QUALITY, 50])


@contextmanager
def video(args, dut):
    with stbt._set_dut_singleton(dut), dut:  # pylint: disable=protected-access
        try:
            yield
        except Exception:  # pylint: disable=W0703
            try:
                _save_screenshot(dut, exception=None,
                                 save_jpg=(args.save_thumbnail != 'never'),
                                 save_png=(args.save_screenshot != 'never'))
            except Exception:  # pylint: disable=broad-except
                pass
            raise
        else:
            _save_screenshot(dut, exception=None,
                             save_jpg=(args.save_thumbnail == 'always'),
                             save_png=(args.save_screenshot == 'always'))


def _import_by_filename(filename_):
    module_dir, module_file = os.path.split(filename_)
    module_name, module_ext = os.path.splitext(module_file)
    if module_ext != '.py':
        raise ImportError("Invalid module filename '%s'" % filename_)
    sys.path = [os.path.abspath(module_dir)] + sys.path
    return __import__(module_name)


_TestFunction = namedtuple(
    "_TestFunction", "script filename funcname line call")


def load_test_function(script, args):
    sys.argv = [script] + args
    if '::' in script:
        filename, funcname = script.split('::', 1)
        module = _import_by_filename(filename)
        function = getattr(module, funcname)
        return _TestFunction(
            script, filename, funcname, function.func_code.co_firstlineno,
            function)
    else:
        filename = os.path.abspath(script)

        test_globals = {
            '__builtins__': __builtins__,
            '__name__': '__main__',
            '__file__': script,
            '__doc__': None,
            '__package__': None,
            'stbt': stbt,
        }

        # For backwards compatibility. We want to encourage people to expli-
        # citly import stbt in their scripts, so don't add new APIs here.
        for x in '''press press_until_match wait_for_match wait_for_motion
                    detect_match MatchResult Position detect_motion
                    MotionResult save_frame get_frame MatchParameters
                    debug UITestError UITestFailure MatchTimeout MotionTimeout
                    ConfigurationError'''.split():
            test_globals[x] = getattr(stbt, x)

        def fn():
            sys.path.insert(0, os.path.dirname(filename))
            execfile(filename, test_globals)

        return _TestFunction(script, script, "", 1, fn)


@contextmanager
def sane_unicode_and_exception_handling(script):
    """
    Exit 1 on failure, and 2 on error.  Print the traceback assuming UTF-8.
    """
    # Simulates python3's defaulting to utf-8 output so we don't get confusing
    # `UnicodeEncodeError`s when printing unicode characters:
    from kitchen.text.converters import getwriter, exception_to_bytes, to_bytes
    if sys.stdout.encoding is None:
        sys.stdout = getwriter('utf8')(sys.stdout)
    if sys.stderr.encoding is None:
        sys.stderr = getwriter('utf8')(sys.stderr)

    try:
        yield
    except Exception as e:  # pylint: disable=W0703
        error_message = exception_to_bytes(e)
        if not error_message and isinstance(e, AssertionError):
            error_message = traceback.extract_tb(sys.exc_info()[2])[-1][3]
        sys.stdout.write("FAIL: %s: %s: %s\n" % (
            script, type(e).__name__, error_message))

        # This is a hack to allow printing exceptions that have unicode messages
        # attached to them.  The default behaviour of Python 2.7 is to replace
        # unicode charactors with \x023-like backslash escapes.  Instead we
        # format them as utf-8 bytes
        #
        # It's not thread-safe, but will only be called at the end of execution:
        traceback._some_str = to_bytes  # pylint: disable=protected-access
        traceback.print_exc(file=sys.stderr)

        # 1 is failure and 2 is error
        if isinstance(e, (stbt.UITestFailure, AssertionError)):
            sys.exit(1)  # Failure
        else:
            sys.exit(2)  # Error


@contextmanager
def tracing(save_trace_arg, test_function):
    _tracer = new_state_sender(save_trace_arg)  # pylint: disable=W0212

    absfilename = os.path.abspath(test_function.filename)

    def tracefunc(frame_, event, _):
        if event == "line" and frame_.f_code.co_filename == absfilename:
            _tracer.log_current_line(frame_.f_code.co_filename, frame_.f_lineno)
        return tracefunc

    _tracer.log_test_starting(test_function)
    sys.settrace(tracefunc)

    try:
        yield
    finally:
        sys.settrace(None)
        _tracer.log_test_ended()
        _tracer.close()
