# Copyright (c) 2007-2008 The PyAMF Project.
# See LICENSE for details.

"""
Twisted server implementation.

This gateway allows you to expose functions in Twisted to AMF
clients and servers.

@see: U{Twisted homepage (external)<http://twistedmatrix.com>}

@author: U{Thijs Triemstra<mailto:info@collab.nl>}
@author: U{Nick Joyce<mailto:nick@boxdesign.co.uk>}

@since: 0.1.0
"""

import sys, os.path

try:
    sys.path.remove('')
except ValueError:
    pass

try:
    sys.path.remove(os.path.dirname(os.path.abspath(__file__)))
except ValueError:
    pass

twisted = __import__('twisted')
__import__('twisted.internet.defer')
__import__('twisted.internet.threads')
__import__('twisted.web.resource')
__import__('twisted.web.server')

defer = twisted.internet.defer
threads = twisted.internet.threads
resource = twisted.web.resource
server = twisted.web.server

import pyamf
from pyamf import remoting
from pyamf.remoting import gateway, amf0, amf3

__all__ = ['TwistedGateway']

class AMF0RequestProcessor(amf0.RequestProcessor):
    """
    A Twisted friendly implementation of
    L{amf0.RequestProcessor<pyamf.remoting.amf0.RequestProcessor>}
    """

    def __call__(self, request, *args, **kwargs):
        """
        Calls the underlying service method.

        @return: A C{Deferred} that will contain the AMF L{Response}.
        @rtype: C{twisted.internet.defer.Deferred}
        """
        try:
            service_request = self.gateway.getServiceRequest(request, request.target)
        except gateway.UnknownServiceError, e:
            return defer.succeed(self.buildErrorResponse(request))

        response = remoting.Response(None)
        deferred_response = defer.Deferred()

        def eb(failure):
            self.gateway.logger.debug(failure.printTraceback())
            deferred_response.callback(self.buildErrorResponse(
                request, (failure.type, failure.value, failure.tb)))

        def response_cb(result):
            self.gateway.logger.debug("AMF Response: %r" % result)
            response.body = result
            deferred_response.callback(response)

        def preprocess_cb(result):
            d = defer.maybeDeferred(self._getBody, request, response, service_request, **kwargs)
            d.addCallback(response_cb).addErrback(eb)

        def auth_cb(result):
            if result is not True:
                response.status = remoting.STATUS_ERROR
                response.body = remoting.ErrorFault(code='AuthenticationError',
                    description='Authentication failed')

                deferred_response.callback(response)

                return

            d = defer.maybeDeferred(self.gateway.preprocessRequest, service_request, *args, **kwargs)
            d.addCallback(preprocess_cb).addErrback(eb)

        # we have a valid service, now attempt authentication
        d = defer.maybeDeferred(self.authenticateRequest, request, service_request, **kwargs)
        d.addCallback(auth_cb).addErrback(eb)

        return deferred_response

class AMF3RequestProcessor(amf3.RequestProcessor):
    """
    A Twisted friendly implementation of
    L{amf3.RequestProcessor<pyamf.remoting.amf3.RequestProcessor>}
    """

    def _processRemotingMessage(self, amf_request, ro_request, **kwargs):
        ro_response = amf3.generate_acknowledgement(ro_request)
        amf_response = remoting.Response(ro_response, status=remoting.STATUS_OK)

        try:
            service_name = ro_request.operation

            if hasattr(ro_request, 'destination') and ro_request.destination:
                service_name = '%s.%s' % (ro_request.destination, service_name)

            service_request = self.gateway.getServiceRequest(amf_request, service_name)
        except gateway.UnknownServiceError, e:
            return defer.succeed(remoting.Response(self.buildErrorResponse(ro_request), status=remoting.STATUS_ERROR))

        deferred_response = defer.Deferred()

        def eb(failure):
            self.gateway.logger.debug(failure.printTraceback())
            ro_response = self.buildErrorResponse(ro_request, (failure.type, failure.value, failure.tb))
            deferred_response.callback(remoting.Response(ro_response, status=remoting.STATUS_ERROR))

        def response_cb(result):
            self.gateway.logger.debug("AMF Response: %r" % result)
            ro_response.body = result
            deferred_response.callback(remoting.Response(ro_response))

        def process_cb(result):
            d = defer.maybeDeferred(self.gateway.callServiceRequest, service_request, *ro_request.body, **kwargs)
            d.addCallback(response_cb).addErrback(eb)

        d = defer.maybeDeferred(self.gateway.preprocessRequest, service_request, *ro_request.body, **kwargs)
        d.addCallback(process_cb).addErrback(eb)

        return deferred_response

    def __call__(self, amf_request, **kwargs):
        """
        Calls the underlying service method.

        @return: A C{deferred} that will contain the AMF L{Response}.
        @rtype: C{Deferred<twisted.internet.defer.Deferred>}
        """
        deferred_response = defer.Deferred()
        ro_request = amf_request.body[0]

        def cb(amf_response):
            deferred_response.callback(amf_response)

        def eb(failure):
            self.gateway.logger.debug(failure.printTraceback())
            deferred_response.callback(self.buildErrorResponse(ro_request,
                (failure.type, failure.value, failure.tb)))

        d = defer.maybeDeferred(self._getBody, amf_request, ro_request, **kwargs)
        d.addCallback(cb).addErrback(eb)

        return deferred_response

class TwistedGateway(gateway.BaseGateway, resource.Resource):
    """
    Twisted Remoting gateway for C{twisted.web}.

    @ivar expose_request: Forces the underlying HTTP request to be the first
        argument to any service call.
    @type expose_request: C{bool}
    """

    allowedMethods = ('POST',)

    def __init__(self, *args, **kwargs):
        if 'expose_request' not in kwargs:
            kwargs['expose_request'] = True

        gateway.BaseGateway.__init__(self, *args, **kwargs)
        resource.Resource.__init__(self)

    def _finaliseRequest(self, request, status, content, mimetype='text/plain'):
        """
        Finalises the request.

        @param request: The HTTP Request.
        @type request: C{http.Request}
        @param status: The HTTP status code.
        @type status: C{int}
        @param content: The content of the response.
        @type content: C{str}
        @param mimetype: The MIME type of the request.
        @type mimetype: C{str}
        """
        request.setResponseCode(status)

        request.setHeader("Content-Type", mimetype)
        request.setHeader("Content-Length", str(len(content)))

        request.write(content)
        request.finish()

    def render_POST(self, request):
        """
        Read remoting request from the client.

        @type request: The HTTP Request.
        @param request: C{twisted.web.http.Request}
        """
        def handleDecodeError(failure):
            """
            Return HTTP 400 Bad Request.
            """
            self.logger.debug(failure.printDetailedTraceback())

            body = "400 Bad Request\n\nThe request body was unable to " \
                "be successfully decoded."

            if self.debug:
                body += "\n\nTraceback:\n\n%s" % failure.printTraceback()

            self._finaliseRequest(request, 400, body)

        request.content.seek(0, 0)
        context = pyamf.get_context(pyamf.AMF0)

        d = threads.deferToThread(remoting.decode, request.content.read(), context)

        def cb(amf_request):
            self.logger.debug("AMF Request: %r" % amf_request)
            x = self.getResponse(request, amf_request)

            x.addCallback(self.sendResponse, request, context)

        # Process the request
        d.addCallback(cb).addErrback(handleDecodeError)

        return server.NOT_DONE_YET

    def sendResponse(self, amf_response, request, context):
        def cb(result):
            self._finaliseRequest(request, 200, result.getvalue(),
                remoting.CONTENT_TYPE)

        def eb(failure):
            """
            Return 500 Internal Server Error.
            """
            self.logger.debug(failure.printDetailedTraceback())

            body = "500 Internal Server Error\n\nThere was an error encoding" \
                " the response."

            if self.debug:
                body += "\n\nTraceback:\n\n%s" % failure.printTraceback()

            self._finaliseRequest(request, 500, body)

        d = threads.deferToThread(remoting.encode, amf_response, context)

        d.addCallback(cb).addErrback(eb)

    def getProcessor(self, request):
        """
        Determines the request processor, based on the request.

        @param request: The AMF message.
        @type request: L{Request<pyamf.remoting.Request>}
        """
        if request.target == 'null':
            return AMF3RequestProcessor(self)

        return AMF0RequestProcessor(self)

    def getResponse(self, http_request, amf_request):
        """
        Processes the AMF request, returning an AMF L{Response}.

        @param http_request: The underlying HTTP Request
        @type http_request: C{twisted.web.http.Request}
        @param amf_request: The AMF Request.
        @type amf_request: L{Envelope<pyamf.remoting.Envelope>}
        """
        response = remoting.Envelope(amf_request.amfVersion, amf_request.clientType)
        dl = []

        def cb(body, name):
            response[name] = body

        for name, message in amf_request:
            processor = self.getProcessor(message)

            d = defer.maybeDeferred(processor, message, http_request=http_request)
            d.addCallback(cb, name)

            dl.append(d)

        def cb2(result):
            return response

        d = defer.DeferredList(dl)

        return d.addCallback(cb2)

    def authenticateRequest(self, service_request, username, password, **kwargs):
        """
        Processes an authentication request. If no authenticator is supplied,
        then authentication succeeds.

        @return: C{Deferred}.
        @rtype: C{twisted.internet.defer.Deferred}
        """
        authenticator = self.getAuthenticator(service_request)
        self.logger.debug('Authenticator expands to: %r' % authenticator)

        if authenticator is None:
            return defer.succeed(True)

        args = (username, password)

        if hasattr(authenticator, '_pyamf_expose_request'):
            http_request = kwargs.get('http_request', None)
            args = (http_request,) + args

        return defer.maybeDeferred(authenticator, *args)

    def preprocessRequest(self, service_request, *args, **kwargs):
        """
        Preprocesses a request.
        """
        processor = self.getPreprocessor(service_request)
        self.logger.debug('Preprocessor expands to: %r' % processor)

        if processor is None:
            return

        args = (service_request,) + args

        if hasattr(processor, '_pyamf_expose_request'):
            http_request = kwargs.get('http_request', None)
            args = (http_request,) + args

        return defer.maybeDeferred(processor, *args)
