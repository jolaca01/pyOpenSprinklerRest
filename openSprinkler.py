import requests
from hashlib import md5
import logging
import colorlog
import datetime

STATUS_SUCCESS = 1

STATUS_CODES = {1:'Success',
                2:'Unauthorized (e.g. missing password or password is incorrect)',
                3:'Mismatch (e.g. new password and confirmation password do not match)',
                16:'Data Missing (e.g. missing required parameters)',
                17:'Out of Range (e.g. value exceeds the acceptable range)',
                18:'Data Format Error (e.g. provided data does not match required format)',
                19:'RF code error (e.g. RF code does not match required format)',
                32:'Page Not Found (e.g. page not found or requested file missing)',
                48:'Not Permitted (e.g. cannot operate on the requested station)'}

class FieldDescriptor(object):
  def __init__(self, tag, type):
    self._tag = tag
    self._type = type

class FieldGetDescriptor(FieldDescriptor):
  def getAsType(self, data):
    return self._type(data[self._tag])

class FieldSetDescriptor(FieldDescriptor):
  def setAsType(self, data):
    data[self._tag] = self._type(data)

def OSDateTime(ts):
  return datetime.datetime.fromtimestamp(ts)

def SunTime(minutes):
  if minutes == 0:
    return None
  now = datetime.datetime.now()
  midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
  return midnight + datetime.timedelta(days=1) - datetime.timedelta(minutes=minutes)

def IPAddress(ip):
  return '%d.%d.%d.%d' % (ip>>24, (ip>>16)&0xFF, (ip>>8)&0xFF, ip&0xFF)

def Stations(stat):
  retval = []
  for i in range(8):
    retval.append(stat & (1<<i))
  return tuple(retval)

def Nop(val):
  return val

def RainDelaySet(dt):
  if not dt:
    return 0
  now = datetime.datetime.now()
  return int((dt - now).total_seconds() / 60)


class Controller:
  '''
  - devt: Device time (epoch time). This is always the local time.
  - nbrd: Number of 8-station boards (including main controller).
  - en: Operation enable bit.
  - rd: Rain delay bit (1: rain delay is currently in effect; 0: no rain delay).
  - rs: Rain sensor status bit (1: rain is detected from rain sensor; 0: no rain detected).
  - rdst: Rain delay stop time (0: rain delay no in effect; otherwise: the time when rain delay is over).
  - loc: Location string.
  - wtkey: Wunderground API key.
  - sunrise: Today’s sunrise time (minutes from midnight).
  - sunset: Today’s sunset time (minutes from midnight).
  - eip: external IP, calculated as (ip[3]<<24)+(ip[2]<<16)+(ip[1]<<8)+ip[0]
  - lwc: last weather call/query (epoch time)
  - lswc: last successful weather call/query (epoch time)
  - sbits: Station status bits. Each byte in this array corresponds to an 8-station board and represents the bit field (LSB).
    For example, 1 means the 1st station on the board is open, 192 means the 7th and 8th stations are open.
  - ps: Program status data: each element is a 3-field array that stores the [pid,rem,start] of a station, where
    pid is the program index (0 means non), rem is the remaining water time (in seconds), start is the start time.
    If a station is not running (sbit is 0) but has a non-zero pid, that means the station is in the queue waiting to run.
  - lrun: Last run record, which stores the [station index, program index, duration, end time] of the last run station.
  '''
  my_get_args = {'device_time': FieldGetDescriptor('devt', OSDateTime),
                 'board_count': FieldGetDescriptor('nbrd', int),
                 'enable': FieldGetDescriptor('en', bool),
                 'rain_delay': FieldGetDescriptor('rd', bool),
                 'rain_sensor': FieldGetDescriptor('rs', bool),
                 'rain_resume': FieldGetDescriptor('rdst', str),  # TODO: figure out what this data type is
                 'location': FieldGetDescriptor('loc', str),
                 'weather_id': FieldGetDescriptor('wtkey', str),
                 'sunrise': FieldGetDescriptor('sunrise', SunTime),
                 'sunset': FieldGetDescriptor('sunset', SunTime),
                 'external_ip': FieldGetDescriptor('eip', IPAddress),
                 'last_weather': FieldGetDescriptor('lwc', OSDateTime),
                 'last_good_weather': FieldGetDescriptor('lswc', OSDateTime),
                 'station_status': FieldGetDescriptor('sbits', Stations),
                 'program_status': FieldGetDescriptor('ps', Nop), # TODO: figure out this data type
                 'last_run': FieldGetDescriptor('lrun', Nop)} # TODO: figure out this data type

  '''
  - rsn: Reset all stations (i.e. stop all stations immediately, including those waiting to run). Binary value.
  - rbt: Reboot the controller. Binary value.
  - en: Operation enable. Binary value.
  - rd: Set rain delay time (in hours). A value of 0 turns off rain delay.
  - re: Set the controller to remote extension mode (so that stations on this controller can be used as remote stations).
  '''
  my_set_args = {'reset_all': FieldSetDescriptor('rsn', int),
                 'reboot': FieldSetDescriptor('rbt', int),
                 'enable': FieldSetDescriptor('en', int),
                 'rain_delay': FieldSetDescriptor('rd', RainDelaySet),
                 'remote_extension': FieldSetDescriptor('re', int)}

  def __init__(self, p):
    self.parent = p

  def __getattr__(self, name):
    if name in self.my_get_args.keys():
      data = self.parent._json_get('jc')
      return self.my_get_args[name].getAsType(data)

  def __setattr__(self, name, value):
    if name in self.my_set_args.keys():
      data = {}
      self.my_set_args[name].setAsType(data)
      self.parent._json_get('cv', data)
    else:
      super().__setattr__(name, value)


class OpenSprinkler:

  def __init__(self, hostname, password, log=None):
    if log is None:
      self.log = logging.getLogger(self.__class__.__name__)
    else:
      self.log = log.getChild(self.__class__.__name__)
    
    self.log.debug('Creating OpenSprinkler object')
    self.hostname = hostname
    self.password = md5(password.encode('utf-8')).hexdigest()

    self.controller = Controller(self)

  def _json_get(self, path, variables=None):
    requests_str = "http://%s/%s/?pw=%s" % (self.hostname, 
                                            path,
                                            self.password)

    if variables:
      for k,v in variables.items():
        requests_str += '&' + str(k) + '=' + str(v)

    r = requests.get(requests_str) 

    self.log.debug('GET %s status: %d', requests_str, r.status_code)
    if r.status_code != 200:
      raise ValueError('Failed GET request with status %d.', r.status_code)

    retval = r.json()
    if 'result' in retval and retval['result'] != STATUS_SUCCESS:
      raise ValueError('Failure response (%d):%s', 
                       retval['result'], 
                       STATUS_CODES[retval['result']])

    return retval


if __name__ == "__main__":
  import sys

  handler = logging.StreamHandler()
  handler.setFormatter(colorlog.ColoredFormatter(
                       '%(log_color)s%(levelname)s:%(name)s:%(message)s'))

  log = colorlog.getLogger('Open Sprinkler Example')
  log.addHandler(handler)
  log.setLevel(logging.DEBUG)

  log.info('Open Sprinkler Example')

  if len(sys.argv) < 3:
    exit(1)
  
  hostname = sys.argv[1]
  password = sys.argv[2]
  os_device = OpenSprinkler(hostname, password, log=log)

  for prop in Controller.my_get_args.keys():
    print('%s: %r' % (prop, getattr(os_device.controller, prop)))

  # log.info('Setting rain delay for 1 hour')
  # os_device.cv.rain_delay = 1
  #
  # print(os_device.jc)
  #
  # log.info('Setting rain delay to 0')
  # os_device.cv.rain_delay = 0
  #
  # print(os_device.jc)

