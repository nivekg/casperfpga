import logging
import katcp
import time
import os
import threading
import Queue
import random

import async_requester
from casperfpga import CasperFpga
from utils import parse_fpg, create_meta_dictionary

LOGGER = logging.getLogger(__name__)


def sendfile(filename, targethost, port, result_queue, timeout=2):
    """
    Send a file to a host using sockets. Place the result of the action in a Queue.Queue
    :param filename: the file to send
    :param targethost: the host to which it must be sent
    :param port: the port the host should open
    :param result_queue: the result of the upload, nothing '' indicates success
    :return:
    """
    import socket
    upload_socket = socket.socket()
    stime = time.time()
    connected = False
    while (not connected) and (time.time() - stime < timeout):
        try:
            upload_socket.connect((targethost, port))
            connected = True
        except:
            time.sleep(0.1)
    if not connected:
        result_queue.put('Could not connect to upload port.')
    try:
        upload_socket.send(open(filename).read())
    except:
        result_queue.put('Could not send file to upload port.')
    result_queue.put('')
    return


class KatcpFpga(CasperFpga, async_requester.AsyncRequester, katcp.CallbackClient):

    def __init__(self, host, port=7147, timeout=2.0, connect=True):
        async_requester.AsyncRequester.__init__(self, host, self.callback_request, max_requests=100)
        katcp.CallbackClient.__init__(self, host, port, tb_limit=20, timeout=timeout,
                                      logger=LOGGER, auto_reconnect=True)
        CasperFpga.__init__(self, host)
        self.system_info = {'last_programmed': '', 'system_name': None, 'program_filename': ''}
        self.unhandled_inform_handler = None
        self._timeout = timeout
        if connect:
            self.connect()
        LOGGER.info('%s:%s created%s.', self.host, port, ' & daemon started' if connect else '')

    def connect(self, timeout=1):
        """
        Establish a connection to the KATCP server on the device.
        :param timeout: How many seconds should we wait?
        :return:
        """
        if not self.is_connected():
            self.start(daemon=True)
            self.wait_connected(timeout)
        if not self.is_connected():
            raise RuntimeError('Could not connect to KATCP server %s' % self.host)
        LOGGER.info('%s: connection established', self.host)

    def disconnect(self):
        """
        Disconnect from the device server.
        :return:
        """
        super(KatcpFpga, self).stop()
        self.join(timeout=self._timeout)
        LOGGER.info('%s: disconnected', self.host)

    def katcprequest(self, name, request_timeout=-1.0, require_ok=True, request_args=()):
        """
        Make a blocking request to the KATCP server and check the result.
        Raise an error if the reply indicates a request failure.
        :param name: request message to send.
        :param request_timeout: number of seconds after which the request must time out
        :param require_ok: will we raise an exception on a response != ok
        :param request_args: request arguments.
        :return: tuple of reply and informs
        """
        # TODO raise sensible errors
        if request_timeout == -1:
            request_timeout = self._timeout
        request = katcp.Message.request(name, *request_args)
        reply, informs = self.blocking_request(request, timeout=request_timeout)
        if (reply.arguments[0] != katcp.Message.OK) and require_ok:
            raise RuntimeError('Request %s on host %s failed.\n\tRequest: %s\n\tReply: %s' %
                               (request.name, self.host, request, reply))
        return reply, informs

    def listdev(self):
        """
        Get a list of the memory bus items in this design.
        :return: a list of memory devices
        """
        _, informs = self.katcprequest(name='listdev', request_timeout=self._timeout)
        return [i.arguments[0] for i in informs]

    def listbof(self):
        """
        Return a list of binary files stored on the host device.
        :return: a list of binary files
        """
        _, informs = self.katcprequest(name='listbof', request_timeout=self._timeout)
        return [i.arguments[0] for i in informs]

    def status(self):
        """
        Return the output of the 'status' request
        :return: FPGA status
        """
        reply, _ = self.katcprequest(name='status', request_timeout=self._timeout)
        return reply.arguments[1]

    def ping(self):
        """
        Use the 'watchdog' request to ping the FPGA host.
        :return: True or False
        """
        reply, _ = self.katcprequest(name='watchdog', request_timeout=self._timeout)
        if reply.arguments[0] == 'ok':
            LOGGER.info('%s: katcp ping okay' % self.host)
            return True
        else:
            LOGGER.error('%s: katcp ping fail' % self.host)
            return False

    def is_running(self):
        """
        Is the FPGA programmed and running?
        :return: True or False
        """
        reply, _ = self.katcprequest(name='fpgastatus', request_timeout=self._timeout, require_ok=False)
        if reply.arguments[0] == 'ok':
            return True
        else:
            return False

    def read(self, device_name, size, offset=0):
        """
        Return size_bytes of binary data with carriage-return escape-sequenced.
        :param device_name: name of memory device from which to read
        :param size: how many bytes to read
        :param offset: start at this offset
        :return: binary data string
        """
        reply, _ = self.katcprequest(name='read', request_timeout=self._timeout, require_ok=True,
                                     request_args=(device_name, str(offset), str(size)))
        return reply.arguments[1]

    def blindwrite(self, device_name, data, offset=0):
        """
        Unchecked data write.
        :param device_name: the memory device to which to write
        :param data: the byte string to write
        :param offset: the offset, in bytes, at which to write
        :return: <nothing>
        """
        assert(type(data) == str), 'You need to supply binary packed string data!'
        assert(len(data) % 4) == 0, 'You must write 32-bit-bounded words!'
        assert((offset % 4) == 0), 'You must write 32-bit-bounded words!'
        self.katcprequest(name='write', request_timeout=self._timeout, require_ok=True,
                          request_args=(device_name, str(offset), data))

    def bulkread(self, device_name, size, offset=0):
        """
        Return size_bytes of binary data with carriage-return escape-sequenced.
        Uses much fast bulkread katcp command which returns data in pages
        using informs rather than one read reply, which has significant buffering
        overhead on the ROACH.
        :param device_name: name of the memory device from which to read
        :param size: how many bytes to read
        :param offset: the offset at which to read
        :return: binary data string
        """
        _, informs = self.katcprequest(name='bulkread', request_timeout=self._timeout, require_ok=True,
                                       request_args=(device_name, str(offset), str(size)))
        return ''.join([i.arguments[0] for i in informs])

    def program(self, filename=None):
        """
        Program the FPGA with the specified binary file.
        :param filename: name of file to program, can vary depending on the formats
                         supported by the device. e.g. fpg, bof, bin
        :return:
        """
        # TODO - The logic here is for broken TCPBORPHSERVER - needs to be fixed.
        if filename is None:
            filename = self.system_info['program_filename']
        elif filename != self.system_info['program_filename']:
            LOGGER.error('Programming filename %s, configured programming filename %s', filename,
                         self.system_info['program_filename'])
        unhandled_informs = []

        # set the unhandled informs callback
        self.unhandled_inform_handler = lambda msg: unhandled_informs.append(msg)
        reply, _ = self.katcprequest(name='progdev', request_timeout=10, request_args=(filename, ))
        self.unhandled_inform_handler = None
        if reply.arguments[0] == 'ok':
            complete_okay = False
            for inf in unhandled_informs:
                if (inf.name == 'fpga') and (inf.arguments[0] == 'ready'):
                    complete_okay = True
            if not complete_okay:
                LOGGER.error('%s: programming %s failed.' % (self.host, filename))
                for inf in unhandled_informs:
                    LOGGER.debug(inf)
                raise RuntimeError('%s: programming %s failed.' % (self.host, filename))
            self.system_info['last_programmed'] = filename
        else:
            LOGGER.error('%s: progdev request %s failed.' % (self.host, filename))
            raise RuntimeError('%s: progdev request %s failed.' % (self.host, filename))
        self.get_system_information()
        LOGGER.info('%s: programmed %s okay.' % (self.host, filename))
        return

    def deprogram(self):
        """
        Deprogram the FPGA.
        :return:
        """
        reply, _ = self.katcprequest(name='progdev')
        if reply.arguments[0] == 'ok':
            super(KatcpFpga, self).deprogram()
        else:
            raise RuntimeError('Could not deprogram FPGA, katcp request failed!')
        LOGGER.info('%s: deprogrammed okay', self.host)

    def upload_to_ram_and_program(self, filename, port=-1, timeout=10, wait_complete=True):
        """
        Upload an FPG file to RAM and then program the FPGA.
        :param filename: the file to upload
        :param port: the port to use on the rx end, -1 means a random port
        :param timeout: how long to wait, seconds
        :param wait_complete: wait for the transaction to complete, return after upload if False
        :return:
        """
        # does the file that is to be uploaded exist on the local filesystem?
        os.path.getsize(filename)

        # function to make the request to the KATCP server
        def makerequest(result_queue):
            try:
                result = self.katcprequest(name='progremote', request_timeout=timeout, require_ok=True,
                                           request_args=(port, ))
                if result[0].arguments[0] == katcp.Message.OK:
                    result_queue.put('')
                else:
                    result_queue.put('Request to client returned, but not Message.OK.')
            except:
                result_queue.put('Request to client failed.')

        if port == -1:
            port = random.randint(2000, 2500)

        # start the request thread and join
        request_queue = Queue.Queue()
        request_thread = threading.Thread(target=makerequest, args=(request_queue, ))
        old_timeout = self._timeout
        self._timeout = timeout
        request_thread.start()
        request_thread.join()
        request_result = request_queue.get()
        if request_result != '':
            raise RuntimeError('progremote request(%s) failed' % request_result)

        # start the upload thread and join
        upload_queue = Queue.Queue()
        unhandled_informs_queue = Queue.Queue()
        upload_thread = threading.Thread(target=sendfile, args=(filename, self.host, port, upload_queue, ))
        self.unhandled_inform_handler = lambda msg: unhandled_informs_queue.put(msg)
        upload_thread.start()
        if not wait_complete:
            self.unhandled_inform_handler = None
            self._timeout = old_timeout
            return
        upload_thread.join()  # wait for the file upload to complete
        upload_result = upload_queue.get()
        if upload_result != '':
            raise RuntimeError('upload(%s)' % upload_result)
        # wait for the '#fpga ready' inform
        done = False
        while not done:
            try:
                inf = unhandled_informs_queue.get(block=True, timeout=timeout)
            except Queue.Empty:
                LOGGER.error('No programming informs yet. Odd?')
                raise RuntimeError('No programming informs yet.')
            if (inf.name == 'fpga') and (inf.arguments[0] == 'ready'):
                done = True
        self.unhandled_inform_handler = None
        self._timeout = old_timeout
        self.system_info['last_programmed'] = filename
        self.get_system_information()
        return

    def upload_to_flash(self, binary_file, port=-1, force_upload=False, timeout=30, wait_complete=True):
        """
        Upload the provided binary file to the flash filesystem.
        :param binary_file: filename of the binary file to upload
        :param port: host-side port, -1 means a random port will be used
        :param force_upload: upload the binary even if it already exists on the host
        :param timeout: upload timeout, in seconds
        :param wait_complete: wait for the upload to complete, or just kick it off
        :return:
        """
        # does the bof file exist?
        os.path.getsize(binary_file)

        # is it on the FPGA already?
        filename = binary_file.split('/')[-1]
        if not force_upload:
            bofs = self.listbof()
            if bofs.count(filename) == 1:
                return

        # function to make the request to the KATCP server
        def makerequest(result_queue):
            try:
                result = self.katcprequest(name='saveremote', request_timeout=timeout, require_ok=True,
                                           request_args=(port, filename, ))
                if result[0].arguments[0] == katcp.Message.OK:
                    result_queue.put('')
                else:
                    result_queue.put('Request to client returned, but not Message.OK.')
            except:
                result_queue.put('Request to client failed.')

        if port == -1:
            port = random.randint(2000, 2500)

        # request thread
        request_queue = Queue.Queue()
        request_thread = threading.Thread(target=makerequest, args=(request_queue, ))
        # upload thread
        upload_queue = Queue.Queue()
        upload_thread = threading.Thread(target=sendfile, args=(binary_file, self.host, port, upload_queue, ))
        # start the threads and join
        old_timeout = self._timeout
        self._timeout = timeout
        request_thread.start()
        upload_thread.start()
        if not wait_complete:
            self._timeout = old_timeout
            return
        request_thread.join()
        self._timeout = old_timeout
        request_result = request_queue.get()
        upload_result = upload_queue.get()
        if (request_result != '') or (upload_result != ''):
            raise Exception('Error: request(%s), upload(%s)' % (request_result, upload_result))
        return

    def _delete_bof(self, filename):
        """
        Delete a binary file from the device.
        :param filename: the file to delete
        :return:
        """
        if filename == 'all':
            bofs = self.listbof()
        else:
            bofs = [filename]
        for bof in bofs:
            result = self.katcprequest(name='delbof', request_timeout=self._timeout, require_ok=True,
                                       request_args=(bof, ))
            if result[0].arguments[0] != katcp.Message.OK:
                raise RuntimeError('Failed to delete bof file %s' % bof)

    def tap_arp_reload(self):
        """
        Have the tap driver reload its ARP table right now.
        :return:
        """
        reply, _ = self.katcprequest(name='tap-arp-reload', request_timeout=-1, require_ok=True)
        if reply.arguments[0] != 'ok':
            raise RuntimeError('%s: failure requesting ARP reload.' % self.host)

    def __str__(self):
        return 'KatcpFpga(%s):%i - %s' % (self.host, self._bindaddr[1],
                                          'connected' if self.is_connected() else 'disconnected')

    def _read_design_info_from_host(self, device=None):
        """
        Katcp request for extra system information embedded in the boffile.
        :param device: can specify a device name if you don't want everything
        :return: a dictionary of metadata
        """
        if device is None:
            reply, informs = self.katcprequest(name='meta', request_timeout=self._timeout, require_ok=True)
        else:
            reply, informs = self.katcprequest(name='meta', request_timeout=self._timeout, require_ok=True,
                                               request_args=(device, ))
        if reply.arguments[0] != 'ok':
            raise RuntimeError('Could not read meta information from %s' % self.host)
        metalist = []
        for inform in informs:
            if len(inform.arguments) != 4:
                raise ValueError('Incorrect number of meta inform arguments: %s' % str(inform.arguments))
            for arg in inform.arguments:
                arg = arg.replace('\_', ' ')
            name, tag, param, value = inform.arguments[0], inform.arguments[1], inform.arguments[2], inform.arguments[3]
            name = name.replace('/', '_')
            metalist.append((name, tag, param, value))
        return create_meta_dictionary(metalist)

    def get_system_information(self, filename=None, fpg_info=None):
        """
        Get information about the design running on the FPGA.
        If filename is given, get it from there, otherwise query the host via KATCP.
        :param filename: fpg filename
        :return: <nothing> the information is populated in the class
        """
        if (not self.is_running()) and (filename is None):
            raise RuntimeError('This can only be run on a running device when no file is given.')
        if filename is not None:
            device_info, coreinfo_devices = parse_fpg(filename)
        else:
            device_info = self._read_design_info_from_host()
            coreinfo_devices = {}
            for dev in self.listdev():
                coreinfo_devices[dev] = {'address': -1, 'bytes': -1}
        super(KatcpFpga, self).get_system_information(fpg_info=(device_info, coreinfo_devices))

    def unhandled_inform(self, msg):
        """
        Overloaded from CallbackClient
        What do we do with unhandled KATCP inform messages that this device receives?
        Pass it onto the registered function, if it's not None
        """
        if self.unhandled_inform_handler is not None:
            self.unhandled_inform_handler(msg)

# end