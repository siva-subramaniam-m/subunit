#
#  subunit: extensions to Python unittest to get test results from subprocesses.
#  Copyright (C) 2005  Robert Collins <robertc@robertcollins.net>
#
#  Licensed under either the Apache License, Version 2.0 or the BSD 3-clause
#  license at the users choice. A copy of both licenses are available in the
#  project source as Apache-2.0 and BSD. You may not use this file except in
#  compliance with one of these two licences.
#  
#  Unless required by applicable law or agreed to in writing, software
#  distributed under these licenses is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
#  license you chose for the specific language governing permissions and
#  limitations under that license.
#

"""Subunit - a streaming test protocol

Overview
========

The ``subunit`` Python package provides a number of ``unittest`` extensions
which can be used to cause tests to output Subunit, to parse Subunit streams
into test activity, perform seamless test isolation within a regular test
case and variously sort, filter and report on test runs.


Key Classes
-----------

The ``subunit.TestProtocolClient`` class is a ``unittest.TestResult``
extension which will translate a test run into a Subunit stream.

The ``subunit.ProtocolTestCase`` class is an adapter between the Subunit wire
protocol and the ``unittest.TestCase`` object protocol. It is used to translate
a stream into a test run, which regular ``unittest.TestResult`` objects can
process and report/inspect.

Subunit has support for non-blocking usage too, for use with asyncore or
Twisted. See the ``TestProtocolServer`` parser class for more details.

Subunit includes extensions to the Python ``TestResult`` protocol. These are
all done in a compatible manner: ``TestResult`` objects that do not implement
the extension methods will not cause errors to be raised, instead the extension
will either lose fidelity (for instance, folding expected failures to success
in Python versions < 2.7 or 3.1), or discard the extended data (for extra
details, tags, timestamping and progress markers).

The test outcome methods ``addSuccess``, ``addError``, ``addExpectedFailure``,
``addFailure``, ``addSkip`` take an optional keyword parameter ``details``
which can be used instead of the usual python unittest parameter.
When used the value of details should be a dict from ``string`` to 
``subunit.content.Content`` objects. This is a draft API being worked on with
the Python Testing In Python mail list, with the goal of permitting a common
way to provide additional data beyond a traceback, such as captured data from
disk, logging messages etc.

The ``tags(new_tags, gone_tags)`` method is called (if present) to add or
remove tags in the test run that is currently executing. If called when no
test is in progress (that is, if called outside of the ``startTest``, 
``stopTest`` pair), the the tags apply to all sebsequent tests. If called
when a test is in progress, then the tags only apply to that test.

The ``time(a_datetime)`` method is called (if present) when a ``time:``
directive is encountered in a Subunit stream. This is used to tell a TestResult
about the time that events in the stream occured at, to allow reconstructing
test timing from a stream.

The ``progress(offset, whence)`` method controls progress data for a stream.
The offset parameter is an int, and whence is one of subunit.PROGRESS_CUR,
subunit.PROGRESS_SET, PROGRESS_PUSH, PROGRESS_POP. Push and pop operations
ignore the offset parameter.


Python test support
-------------------

``subunit.run`` is a convenience wrapper to run a Python test suite via
the command line, reporting via Subunit::

  $ python -m subunit.run mylib.tests.test_suite

The ``IsolatedTestSuite`` class is a TestSuite that forks before running its
tests, allowing isolation between the test runner and some tests.

Similarly, ``IsolatedTestCase`` is a base class which can be subclassed to get
tests that will fork() before that individual test is run.

`ExecTestCase`` is a convenience wrapper for running an external 
program to get a Subunit stream and then report that back to an arbitrary
result object::

 class AggregateTests(subunit.ExecTestCase):

     def test_script_one(self):
         './bin/script_one'

     def test_script_two(self):
         './bin/script_two'
 
 # Normally your normal test loading would take of this automatically,
 # It is only spelt out in detail here for clarity.
 suite = unittest.TestSuite([AggregateTests("test_script_one"),
     AggregateTests("test_script_two")])
 # Create any TestResult class you like.
 result = unittest._TextTestResult(sys.stdout)
 # And run your suite as normal, Subunit will exec each external script as
 # needed and report to your result object.
 suite.run(result)
"""

import datetime
import os
import re
from StringIO import StringIO
import subprocess
import sys
import unittest

import iso8601

import content, content_type


PROGRESS_SET = 0
PROGRESS_CUR = 1
PROGRESS_PUSH = 2
PROGRESS_POP = 3


def test_suite():
    import subunit.tests
    return subunit.tests.test_suite()


def join_dir(base_path, path):
    """
    Returns an absolute path to C{path}, calculated relative to the parent
    of C{base_path}.

    @param base_path: A path to a file or directory.
    @param path: An absolute path, or a path relative to the containing
    directory of C{base_path}.

    @return: An absolute path to C{path}.
    """
    return os.path.join(os.path.dirname(os.path.abspath(base_path)), path)


def tags_to_new_gone(tags):
    """Split a list of tags into a new_set and a gone_set."""
    new_tags = set()
    gone_tags = set()
    for tag in tags:
        if tag[0] == '-':
            gone_tags.add(tag[1:])
        else:
            new_tags.add(tag)
    return new_tags, gone_tags


class DiscardStream(object):
    """A filelike object which discards what is written to it."""

    def write(self, bytes):
        pass


class TestProtocolServer(object):
    """A parser for subunit.
    
    :ivar tags: The current tags associated with the protocol stream.
    """

    OUTSIDE_TEST = 0
    TEST_STARTED = 1
    READING_FAILURE = 2
    READING_ERROR = 3
    READING_SKIP = 4
    READING_XFAIL = 5
    READING_SUCCESS = 6

    def __init__(self, client, stream=None):
        """Create a TestProtocolServer instance.

        :param client: An object meeting the unittest.TestResult protocol.
        :param stream: The stream that lines received which are not part of the
            subunit protocol should be written to. This allows custom handling
            of mixed protocols. By default, sys.stdout will be used for
            convenience.
        """
        self.state = TestProtocolServer.OUTSIDE_TEST
        self.client = client
        if stream is None:
            stream = sys.stdout
        self._stream = stream

    def _addError(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addError(self._current_test, RemoteError(""))
            self.client.stopTest(self._current_test)
            self._current_test = None
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_ERROR
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _addExpectedFail(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            xfail = getattr(self.client, 'addExpectedFailure', None)
            if callable(xfail):
                xfail(self._current_test, RemoteError())
            else:
                self.client.addSuccess(self._current_test)
            self.client.stopTest(self._current_test)
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_XFAIL
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _addFailure(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addFailure(self._current_test, RemoteError())
            self.client.stopTest(self._current_test)
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_FAILURE
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _addSkip(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self._skip_or_error()
            self.client.stopTest(self._current_test)
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_SKIP
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _skip_or_error(self, message=None):
        """Report the current test as a skip if possible, or else an error."""
        addSkip = getattr(self.client, 'addSkip', None)
        if not callable(addSkip):
            self.client.addError(self._current_test, RemoteError(message))
        else:
            if not message:
                message = "No reason given"
            addSkip(self._current_test, message)

    def _addSuccess(self, offset, line):
        if (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description == line[offset:-1]):
            self._succeedTest()
        elif (self.state == TestProtocolServer.TEST_STARTED and
            self.current_test_description + " [" == line[offset:-1]):
            self.state = TestProtocolServer.READING_SUCCESS
            self._message = ""
        else:
            self.stdOutLineReceived(line)

    def _appendMessage(self, line):
        if line[0:2] == " ]":
            # quoted ] start
            self._message += line[1:]
        else:
            self._message += line

    def endQuote(self, line):
        if self.state == TestProtocolServer.READING_FAILURE:
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addFailure(self._current_test,
                                   RemoteError(self._message))
            self.client.stopTest(self._current_test)
        elif self.state == TestProtocolServer.READING_ERROR:
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self.client.addError(self._current_test,
                                 RemoteError(self._message))
            self.client.stopTest(self._current_test)
        elif self.state == TestProtocolServer.READING_SKIP:
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            self._skip_or_error(self._message)
            self.client.stopTest(self._current_test)
        elif self.state == TestProtocolServer.READING_XFAIL:
            self.state = TestProtocolServer.OUTSIDE_TEST
            self.current_test_description = None
            xfail = getattr(self.client, 'addExpectedFailure', None)
            if callable(xfail):
                xfail(self._current_test, RemoteError(self._message))
            else:
                self.client.addSuccess(self._current_test)
            self.client.stopTest(self._current_test)
        elif self.state == TestProtocolServer.READING_SUCCESS:
            self._succeedTest()
        else:
            self.stdOutLineReceived(line)

    def _handleProgress(self, offset, line):
        """Process a progress directive."""
        line = line[offset:].strip()
        if line[0] in '+-':
            whence = PROGRESS_CUR
            delta = int(line)
        elif line == "push":
            whence = PROGRESS_PUSH
            delta = None
        elif line == "pop":
            whence = PROGRESS_POP
            delta = None
        else:
            whence = PROGRESS_SET
            delta = int(line)
        progress_method = getattr(self.client, 'progress', None)
        if callable(progress_method):
            progress_method(delta, whence)

    def _handleTags(self, offset, line):
        """Process a tags command."""
        tags = line[offset:].split()
        new_tags, gone_tags = tags_to_new_gone(tags)
        tags_method = getattr(self.client, 'tags', None)
        if tags_method is not None:
            tags_method(new_tags, gone_tags)

    def _handleTime(self, offset, line):
        # Accept it, but do not do anything with it yet.
        try:
            event_time = iso8601.parse_date(line[offset:-1])
        except TypeError, e:
            raise TypeError("Failed to parse %r, got %r" % (line, e))
        time_method = getattr(self.client, 'time', None)
        if callable(time_method):
            time_method(event_time)

    def lineReceived(self, line):
        """Call the appropriate local method for the received line."""
        if line == "]\n":
            self.endQuote(line)
        elif self.state in (TestProtocolServer.READING_FAILURE,
            TestProtocolServer.READING_ERROR, TestProtocolServer.READING_SKIP,
            TestProtocolServer.READING_SUCCESS,
            TestProtocolServer.READING_XFAIL
            ):
            self._appendMessage(line)
        else:
            parts = line.split(None, 1)
            if len(parts) == 2:
                cmd, rest = parts
                offset = len(cmd) + 1
                cmd = cmd.strip(':')
                if cmd in ('test', 'testing'):
                    self._startTest(offset, line)
                elif cmd == 'error':
                    self._addError(offset, line)
                elif cmd == 'failure':
                    self._addFailure(offset, line)
                elif cmd == 'progress':
                    self._handleProgress(offset, line)
                elif cmd == 'skip':
                    self._addSkip(offset, line)
                elif cmd in ('success', 'successful'):
                    self._addSuccess(offset, line)
                elif cmd in ('tags',):
                    self._handleTags(offset, line)
                elif cmd in ('time',):
                    self._handleTime(offset, line)
                elif cmd == 'xfail':
                    self._addExpectedFail(offset, line)
                else:
                    self.stdOutLineReceived(line)
            else:
                self.stdOutLineReceived(line)

    def _lostConnectionInTest(self, state_string):
        error_string = "lost connection during %stest '%s'" % (
            state_string, self.current_test_description)
        self.client.addError(self._current_test, RemoteError(error_string))
        self.client.stopTest(self._current_test)

    def lostConnection(self):
        """The input connection has finished."""
        if self.state == TestProtocolServer.OUTSIDE_TEST:
            return
        if self.state == TestProtocolServer.TEST_STARTED:
            self._lostConnectionInTest('')
        elif self.state == TestProtocolServer.READING_ERROR:
            self._lostConnectionInTest('error report of ')
        elif self.state == TestProtocolServer.READING_FAILURE:
            self._lostConnectionInTest('failure report of ')
        elif self.state == TestProtocolServer.READING_SUCCESS:
            self._lostConnectionInTest('success report of ')
        elif self.state == TestProtocolServer.READING_SKIP:
            self._lostConnectionInTest('skip report of ')
        elif self.state == TestProtocolServer.READING_XFAIL:
            self._lostConnectionInTest('xfail report of ')
        else:
            self._lostConnectionInTest('unknown state of ')

    def readFrom(self, pipe):
        for line in pipe.readlines():
            self.lineReceived(line)
        self.lostConnection()

    def _startTest(self, offset, line):
        """Internal call to change state machine. Override startTest()."""
        if self.state == TestProtocolServer.OUTSIDE_TEST:
            self.state = TestProtocolServer.TEST_STARTED
            self._current_test = RemotedTestCase(line[offset:-1])
            self.current_test_description = line[offset:-1]
            self.client.startTest(self._current_test)
        else:
            self.stdOutLineReceived(line)

    def stdOutLineReceived(self, line):
        self._stream.write(line)

    def _succeedTest(self):
        self.client.addSuccess(self._current_test)
        self.client.stopTest(self._current_test)
        self.current_test_description = None
        self._current_test = None
        self.state = TestProtocolServer.OUTSIDE_TEST


class RemoteException(Exception):
    """An exception that occured remotely to Python."""

    def __eq__(self, other):
        try:
            return self.args == other.args
        except AttributeError:
            return False


class TestProtocolClient(unittest.TestResult):
    """A TestResult which generates a subunit stream for a test run.
    
    # Get a TestSuite or TestCase to run
    suite = make_suite()
    # Create a stream (any object with a 'write' method)
    stream = file('tests.log', 'wb')
    # Create a subunit result object which will output to the stream
    result = subunit.TestProtocolClient(stream)
    # Optionally, to get timing data for performance analysis, wrap the
    # serialiser with a timing decorator
    result = subunit.test_results.AutoTimingTestResultDecorator(result)
    # Run the test suite reporting to the subunit result object
    suite.run(result)
    # Close the stream.
    stream.close()
    """

    def __init__(self, stream):
        unittest.TestResult.__init__(self)
        self._stream = stream

    def addError(self, test, error=None, details=None):
        """Report an error in test test.
        
        Only one of error and details should be provided: conceptually there
        are two separate methods:
            addError(self, test, error)
            addError(self, test, details)

        :param error: Standard unittest positional argument form - an
            exc_info tuple.
        :param details: New Testing-in-python drafted API; a dict from string
            to subunit.Content objects.
        """
        self._addOutcome("error", test, error=error, details=details)

    def addExpectedFailure(self, test, error=None, details=None):
        """Report an expected failure in test test.
        
        Only one of error and details should be provided: conceptually there
        are two separate methods:
            addError(self, test, error)
            addError(self, test, details)

        :param error: Standard unittest positional argument form - an
            exc_info tuple.
        :param details: New Testing-in-python drafted API; a dict from string
            to subunit.Content objects.
        """
        self._addOutcome("xfail", test, error=error, details=details)

    def addFailure(self, test, error=None, details=None):
        """Report a failure in test test.
        
        Only one of error and details should be provided: conceptually there
        are two separate methods:
            addFailure(self, test, error)
            addFailure(self, test, details)

        :param error: Standard unittest positional argument form - an
            exc_info tuple.
        :param details: New Testing-in-python drafted API; a dict from string
            to subunit.Content objects.
        """
        self._addOutcome("failure", test, error=error, details=details)

    def _addOutcome(self, outcome, test, error=None, details=None):
        """Report a failure in test test.
        
        Only one of error and details should be provided: conceptually there
        are two separate methods:
            addOutcome(self, test, error)
            addOutcome(self, test, details)

        :param outcome: A string describing the outcome - used as the
            event name in the subunit stream.
        :param error: Standard unittest positional argument form - an
            exc_info tuple.
        :param details: New Testing-in-python drafted API; a dict from string
            to subunit.Content objects.
        """
        self._stream.write("%s: %s" % (outcome, test.id()))
        if error is None and details is None:
            raise ValueError
        if error is not None:
            self._stream.write(" [\n")
            for line in self._exc_info_to_string(error, test).splitlines():
                self._stream.write("%s\n" % line)
        else:
            self._write_details(details)
        self._stream.write("]\n")

    def addSkip(self, test, reason=None, details=None):
        """Report a skipped test."""
        if reason is None:
            self._addOutcome("skip", test, error=None, details=details)
        else:
            self._stream.write("skip: %s [\n" % test.id())
            self._stream.write("%s\n" % reason)
            self._stream.write("]\n")

    def addSuccess(self, test, details=None):
        """Report a success in a test."""
        self._stream.write("successful: %s" % test.id())
        if not details:
            self._stream.write("\n")
        else:
            self._write_details(details)
            self._stream.write("]\n")

    def startTest(self, test):
        """Mark a test as starting its test run."""
        self._stream.write("test: %s\n" % test.id())

    def progress(self, offset, whence):
        """Provide indication about the progress/length of the test run.

        :param offset: Information about the number of tests remaining. If
            whence is PROGRESS_CUR, then offset increases/decreases the
            remaining test count. If whence is PROGRESS_SET, then offset
            specifies exactly the remaining test count.
        :param whence: One of PROGRESS_CUR, PROGRESS_SET, PROGRESS_PUSH,
            PROGRESS_POP.
        """
        if whence == PROGRESS_CUR and offset > -1:
            prefix = "+"
        elif whence == PROGRESS_PUSH:
            prefix = ""
            offset = "push"
        elif whence == PROGRESS_POP:
            prefix = ""
            offset = "pop"
        else:
            prefix = ""
        self._stream.write("progress: %s%s\n" % (prefix, offset))

    def time(self, a_datetime):
        """Inform the client of the time.

        ":param datetime: A datetime.datetime object.
        """
        time = a_datetime.astimezone(iso8601.Utc())
        self._stream.write("time: %04d-%02d-%02d %02d:%02d:%02d.%06dZ\n" % (
            time.year, time.month, time.day, time.hour, time.minute,
            time.second, time.microsecond))

    def _write_details(self, details):
        """Output details to the stream.

        :param details: An extended details dict for a test outcome.
        """
        self._stream.write(" [ multipart\n")
        for name, content in sorted(details.iteritems()):
            self._stream.write("Content-Type: %s/%s" %
                (content.content_type.type, content.content_type.subtype))
            parameters = content.content_type.parameters
            if parameters:
                self._stream.write(";")
                param_strs = []
                for param, value in parameters.iteritems():
                    param_strs.append("%s=%s" % (param, value))
                self._stream.write(",".join(param_strs))
            self._stream.write("\n%s\n" % name)
            for bytes in content.iter_bytes():
                self._stream.write("%d\n%s" % (len(bytes), bytes))
            self._stream.write("0\n")

    def done(self):
        """Obey the testtools result.done() interface."""


def RemoteError(description=""):
    if description == "":
        description = "\n"
    return (RemoteException, RemoteException(description), None)


class RemotedTestCase(unittest.TestCase):
    """A class to represent test cases run in child processes.
    
    Instances of this class are used to provide the Python test API a TestCase
    that can be printed to the screen, introspected for metadata and so on.
    However, as they are a simply a memoisation of a test that was actually
    run in the past by a separate process, they cannot perform any interactive
    actions.
    """

    def __eq__ (self, other):
        try:
            return self.__description == other.__description
        except AttributeError:
            return False

    def __init__(self, description):
        """Create a psuedo test case with description description."""
        self.__description = description

    def error(self, label):
        raise NotImplementedError("%s on RemotedTestCases is not permitted." %
            label)

    def setUp(self):
        self.error("setUp")

    def tearDown(self):
        self.error("tearDown")

    def shortDescription(self):
        return self.__description

    def id(self):
        return "%s" % (self.__description,)

    def __str__(self):
        return "%s (%s)" % (self.__description, self._strclass())

    def __repr__(self):
        return "<%s description='%s'>" % \
               (self._strclass(), self.__description)

    def run(self, result=None):
        if result is None: result = self.defaultTestResult()
        result.startTest(self)
        result.addError(self, RemoteError("Cannot run RemotedTestCases.\n"))
        result.stopTest(self)

    def _strclass(self):
        cls = self.__class__
        return "%s.%s" % (cls.__module__, cls.__name__)


class ExecTestCase(unittest.TestCase):
    """A test case which runs external scripts for test fixtures."""

    def __init__(self, methodName='runTest'):
        """Create an instance of the class that will use the named test
           method when executed. Raises a ValueError if the instance does
           not have a method with the specified name.
        """
        unittest.TestCase.__init__(self, methodName)
        testMethod = getattr(self, methodName)
        self.script = join_dir(sys.modules[self.__class__.__module__].__file__,
                               testMethod.__doc__)

    def countTestCases(self):
        return 1

    def run(self, result=None):
        if result is None: result = self.defaultTestResult()
        self._run(result)

    def debug(self):
        """Run the test without collecting errors in a TestResult"""
        self._run(unittest.TestResult())

    def _run(self, result):
        protocol = TestProtocolServer(result)
        output = subprocess.Popen(self.script, shell=True,
            stdout=subprocess.PIPE).communicate()[0]
        protocol.readFrom(StringIO(output))


class IsolatedTestCase(unittest.TestCase):
    """A TestCase which executes in a forked process.
    
    Each test gets its own process, which has a performance overhead but will
    provide excellent isolation from global state (such as django configs,
    zope utilities and so on).
    """

    def run(self, result=None):
        if result is None: result = self.defaultTestResult()
        run_isolated(unittest.TestCase, self, result)


class IsolatedTestSuite(unittest.TestSuite):
    """A TestSuite which runs its tests in a forked process.
    
    This decorator that will fork() before running the tests and report the
    results from the child process using a Subunit stream.  This is useful for
    handling tests that mutate global state, or are testing C extensions that
    could crash the VM.
    """

    def run(self, result=None):
        if result is None: result = unittest.TestResult()
        run_isolated(unittest.TestSuite, self, result)


def run_isolated(klass, self, result):
    """Run a test suite or case in a subprocess, using the run method on klass.
    """
    c2pread, c2pwrite = os.pipe()
    # fixme - error -> result
    # now fork
    pid = os.fork()
    if pid == 0:
        # Child
        # Close parent's pipe ends
        os.close(c2pread)
        # Dup fds for child
        os.dup2(c2pwrite, 1)
        # Close pipe fds.
        os.close(c2pwrite)

        # at this point, sys.stdin is redirected, now we want
        # to filter it to escape ]'s.
        ### XXX: test and write that bit.

        result = TestProtocolClient(sys.stdout)
        klass.run(self, result)
        sys.stdout.flush()
        sys.stderr.flush()
        # exit HARD, exit NOW.
        os._exit(0)
    else:
        # Parent
        # Close child pipe ends
        os.close(c2pwrite)
        # hookup a protocol engine
        protocol = TestProtocolServer(result)
        protocol.readFrom(os.fdopen(c2pread, 'rU'))
        os.waitpid(pid, 0)
        # TODO return code evaluation.
    return result


def TAP2SubUnit(tap, subunit):
    """Filter a TAP pipe into a subunit pipe.
    
    :param tap: A tap pipe/stream/file object.
    :param subunit: A pipe/stream/file object to write subunit results to.
    :return: The exit code to exit with.
    """
    BEFORE_PLAN = 0
    AFTER_PLAN = 1
    SKIP_STREAM = 2
    client = TestProtocolClient(subunit)
    state = BEFORE_PLAN
    plan_start = 1
    plan_stop = 0
    def _skipped_test(subunit, plan_start):
        # Some tests were skipped.
        subunit.write('test test %d\n' % plan_start)
        subunit.write('error test %d [\n' % plan_start)
        subunit.write('test missing from TAP output\n')
        subunit.write(']\n')
        return plan_start + 1
    # Test data for the next test to emit
    test_name = None
    log = []
    result = None
    def _emit_test():
        "write out a test"
        if test_name is None:
            return
        subunit.write("test %s\n" % test_name)
        if not log:
            subunit.write("%s %s\n" % (result, test_name))
        else:
            subunit.write("%s %s [\n" % (result, test_name))
        if log:
            for line in log:
                subunit.write("%s\n" % line)
            subunit.write("]\n")
        del log[:]
    for line in tap:
        if state == BEFORE_PLAN:
            match = re.match("(\d+)\.\.(\d+)\s*(?:\#\s+(.*))?\n", line)
            if match:
                state = AFTER_PLAN
                _, plan_stop, comment = match.groups()
                plan_stop = int(plan_stop)
                if plan_start > plan_stop and plan_stop == 0:
                    # skipped file
                    state = SKIP_STREAM
                    subunit.write("test file skip\n")
                    subunit.write("skip file skip [\n")
                    subunit.write("%s\n" % comment)
                    subunit.write("]\n")
                continue
        # not a plan line, or have seen one before
        match = re.match("(ok|not ok)(?:\s+(\d+)?)?(?:\s+([^#]*[^#\s]+)\s*)?(?:\s+#\s+(TODO|SKIP)(?:\s+(.*))?)?\n", line)
        if match:
            # new test, emit current one.
            _emit_test()
            status, number, description, directive, directive_comment = match.groups()
            if status == 'ok':
                result = 'success'
            else:
                result = "failure"
            if description is None:
                description = ''
            else:
                description = ' ' + description
            if directive is not None:
                if directive == 'TODO':
                    result = 'xfail'
                elif directive == 'SKIP':
                    result = 'skip'
                if directive_comment is not None:
                    log.append(directive_comment)
            if number is not None:
                number = int(number)
                while plan_start < number:
                    plan_start = _skipped_test(subunit, plan_start)
            test_name = "test %d%s" % (plan_start, description)
            plan_start += 1
            continue
        match = re.match("Bail out\!(?:\s*(.*))?\n", line)
        if match:
            reason, = match.groups()
            if reason is None:
                extra = ''
            else:
                extra = ' %s' % reason
            _emit_test()
            test_name = "Bail out!%s" % extra
            result = "error"
            state = SKIP_STREAM
            continue
        match = re.match("\#.*\n", line)
        if match:
            log.append(line[:-1])
            continue
        subunit.write(line)
    _emit_test()
    while plan_start <= plan_stop:
        # record missed tests
        plan_start = _skipped_test(subunit, plan_start)
    return 0


def tag_stream(original, filtered, tags):
    """Alter tags on a stream.

    :param original: The input stream.
    :param filtered: The output stream.
    :param tags: The tags to apply. As in a normal stream - a list of 'TAG' or
        '-TAG' commands.

        A 'TAG' command will add the tag to the output stream,
        and override any existing '-TAG' command in that stream.
        Specifically:
         * A global 'tags: TAG' will be added to the start of the stream.
         * Any tags commands with -TAG will have the -TAG removed.

        A '-TAG' command will remove the TAG command from the stream.
        Specifically:
         * A 'tags: -TAG' command will be added to the start of the stream.
         * Any 'tags: TAG' command will have 'TAG' removed from it.
        Additionally, any redundant tagging commands (adding a tag globally
        present, or removing a tag globally removed) are stripped as a
        by-product of the filtering.
    :return: 0
    """
    new_tags, gone_tags = tags_to_new_gone(tags)
    def write_tags(new_tags, gone_tags):
        if new_tags or gone_tags:
            filtered.write("tags: " + ' '.join(new_tags))
            if gone_tags:
                for tag in gone_tags:
                    filtered.write("-" + tag)
            filtered.write("\n")
    write_tags(new_tags, gone_tags)
    # TODO: use the protocol parser and thus don't mangle test comments.
    for line in original:
        if line.startswith("tags:"):
            line_tags = line[5:].split()
            line_new, line_gone = tags_to_new_gone(line_tags)
            line_new = line_new - gone_tags
            line_gone = line_gone - new_tags
            write_tags(line_new, line_gone)
        else:
            filtered.write(line)
    return 0


class ProtocolTestCase(object):
    """Subunit wire protocol to unittest.TestCase adapter.

    ProtocolTestCase honours the core of ``unittest.TestCase`` protocol -
    calling a ProtocolTestCase or invoking the run() method will make a 'test
    run' happen. The 'test run' will simply be a replay of the test activity
    that has been encoded into the stream. The ``unittest.TestCase`` ``debug``
    and ``countTestCases`` methods are not supported because there isn't a
    sensible mapping for those methods.
    
    # Get a stream (any object with a readline() method), in this case the
    # stream output by the example from ``subunit.TestProtocolClient``.
    stream = file('tests.log', 'rb')
    # Create a parser which will read from the stream and emit 
    # activity to a unittest.TestResult when run() is called.
    suite = subunit.ProtocolTestCase(stream)
    # Create a result object to accept the contents of that stream.
    result = unittest._TextTestResult(sys.stdout)
    # 'run' the tests - process the stream and feed its contents to result.
    suite.run(result)
    stream.close()

    :seealso: TestProtocolServer (the subunit wire protocol parser).
    """

    def __init__(self, stream, passthrough=None):
        """Create a ProtocolTestCase reading from stream.

        :param stream: A filelike object which a subunit stream can be read
            from.
        :param passthrough: A stream pass non subunit input on to. If not
            supplied, the TestProtocolServer default is used.
        """
        self._stream = stream
        self._passthrough = passthrough

    def __call__(self, result=None):
        return self.run(result)

    def run(self, result=None):
        if result is None:
            result = self.defaultTestResult()
        protocol = TestProtocolServer(result, self._passthrough)
        line = self._stream.readline()
        while line:
            protocol.lineReceived(line)
            line = self._stream.readline()
        protocol.lostConnection()


class TestResultStats(unittest.TestResult):
    """A pyunit TestResult interface implementation for making statistics.
    
    :ivar total_tests: The total tests seen.
    :ivar passed_tests: The tests that passed.
    :ivar failed_tests: The tests that failed.
    :ivar seen_tags: The tags seen across all tests.
    """

    def __init__(self, stream):
        """Create a TestResultStats which outputs to stream."""
        unittest.TestResult.__init__(self)
        self._stream = stream
        self.failed_tests = 0
        self.skipped_tests = 0
        self.seen_tags = set()

    @property
    def total_tests(self):
        return self.testsRun

    def addError(self, test, err):
        self.failed_tests += 1

    def addFailure(self, test, err):
        self.failed_tests += 1

    def addSkip(self, test, reason):
        self.skipped_tests += 1

    def formatStats(self):
        self._stream.write("Total tests:   %5d\n" % self.total_tests)
        self._stream.write("Passed tests:  %5d\n" % self.passed_tests)
        self._stream.write("Failed tests:  %5d\n" % self.failed_tests)
        self._stream.write("Skipped tests: %5d\n" % self.skipped_tests)
        tags = sorted(self.seen_tags)
        self._stream.write("Seen tags: %s\n" % (", ".join(tags)))

    @property
    def passed_tests(self):
        return self.total_tests - self.failed_tests - self.skipped_tests

    def tags(self, new_tags, gone_tags):
        """Accumulate the seen tags."""
        self.seen_tags.update(new_tags)

    def wasSuccessful(self):
        """Tells whether or not this result was a success"""
        return self.failed_tests == 0


class TestResultFilter(unittest.TestResult):
    """A pyunit TestResult interface implementation which filters tests.

    Tests that pass the filter are handed on to another TestResult instance
    for further processing/reporting. To obtain the filtered results, 
    the other instance must be interrogated.

    :ivar result: The result that tests are passed to after filtering.
    :ivar filter_predicate: The callback run to decide whether to pass 
        a result.
    """

    def __init__(self, result, filter_error=False, filter_failure=False,
        filter_success=True, filter_skip=False,
        filter_predicate=None):
        """Create a FilterResult object filtering to result.
        
        :param filter_error: Filter out errors.
        :param filter_failure: Filter out failures.
        :param filter_success: Filter out successful tests.
        :param filter_skip: Filter out skipped tests.
        :param filter_predicate: A callable taking (test, err) and 
            returning True if the result should be passed through.
            err is None for success.
        """
        unittest.TestResult.__init__(self)
        self.result = result
        self._filter_error = filter_error
        self._filter_failure = filter_failure
        self._filter_success = filter_success
        self._filter_skip = filter_skip
        if filter_predicate is None:
            filter_predicate = lambda test, err: True
        self.filter_predicate = filter_predicate
        # The current test (for filtering tags)
        self._current_test = None
        # Has the current test been filtered (for outputting test tags)
        self._current_test_filtered = None
        # The (new, gone) tags for the current test.
        self._current_test_tags = None
        
    def addError(self, test, err):
        if not self._filter_error and self.filter_predicate(test, err):
            self.result.startTest(test)
            self.result.addError(test, err)

    def addFailure(self, test, err):
        if not self._filter_failure and self.filter_predicate(test, err):
            self.result.startTest(test)
            self.result.addFailure(test, err)

    def addSkip(self, test, reason):
        if not self._filter_skip and self.filter_predicate(test, reason):
            self.result.startTest(test)
            # This is duplicated, it would be nice to have on a 'calls
            # TestResults' mixin perhaps.
            addSkip = getattr(self.result, 'addSkip', None)
            if not callable(addSkip):
                self.result.addError(test, RemoteError(reason))
            else:
                self.result.addSkip(test, reason)

    def addSuccess(self, test):
        if not self._filter_success and self.filter_predicate(test, None):
            self.result.startTest(test)
            self.result.addSuccess(test)

    def startTest(self, test):
        """Start a test.
        
        Not directly passed to the client, but used for handling of tags
        correctly.
        """
        self._current_test = test
        self._current_test_filtered = False
        self._current_test_tags = set(), set()
    
    def stopTest(self, test):
        """Stop a test.
        
        Not directly passed to the client, but used for handling of tags
        correctly.
        """
        if not self._current_test_filtered:
            # Tags to output for this test.
            if self._current_test_tags[0] or self._current_test_tags[1]:
                tags_method = getattr(self.result, 'tags', None)
                if callable(tags_method):
                    self.result.tags(*self._current_test_tags)
            self.result.stopTest(test)
        self._current_test = None
        self._current_test_filtered = None
        self._current_test_tags = None

    def tags(self, new_tags, gone_tags):
        """Handle tag instructions.

        Adds and removes tags as appropriate. If a test is currently running,
        tags are not affected for subsequent tests.
        
        :param new_tags: Tags to add,
        :param gone_tags: Tags to remove.
        """
        if self._current_test is not None:
            # gather the tags until the test stops.
            self._current_test_tags[0].update(new_tags)
            self._current_test_tags[0].difference_update(gone_tags)
            self._current_test_tags[1].update(gone_tags)
            self._current_test_tags[1].difference_update(new_tags)
        tags_method = getattr(self.result, 'tags', None)
        if tags_method is None:
            return
        return tags_method(new_tags, gone_tags)

    def id_to_orig_id(self, id):
        if id.startswith("subunit.RemotedTestCase."):
            return id[len("subunit.RemotedTestCase."):]
        return id

