# -*- coding: utf8 -*-
#
# Module CACHE
#
# Part of Nutils: open source numerical utilities for Python. Jointly developed
# by HvZ Computational Engineering, TU/e Multiscale Engineering Fluid Dynamics,
# and others. More info at http://nutils.org <info@nutils.org>. (c) 2014

"""
The cache module.
"""

from __future__ import print_function, division
from . import core, log
import os, sys, weakref, numpy


_property = property
def property( f ):
  def cache_property_wrapper( self, f=f ):
    try:
      value = self.__dict__[f.__name__]
    except KeyError:
      value = f( self )
      self.__dict__[f.__name__] = value
    return value
  assert not cache_property_wrapper.__closure__
  return _property(cache_property_wrapper)

class HashableArray( object ):
  # FRAGILE: assumes contents are not going to be changed
  def __init__( self, array ):
    self.array = array
    self.quickdata = array.shape, tuple( array.flat[::array.size//32+1] )
  def __hash__( self ):
    return hash( self.quickdata )
  def __eq__( self, other ):
    # check full array only if we really must
    return isinstance(other,HashableArray) and ( self.array is other.array
      or self.quickdata == other.quickdata and numpy.all( self.array == other.array ) )
  
class HashableList( tuple ):
  def __new__( cls, L ):
    return tuple.__new__( cls, map( _hashable, L ) )

class HashableDict( frozenset ):
  def __new__( cls, D ):
    return frozenset.__new__( cls, map( _hashable, D.items() ) )

class HashableAny( object ):
  def __init__( self, obj ):
    self.obj = obj
  def __hash__( self ):
    return hash( id(self.obj) )
  def __eq__( self, other ):
    return isinstance(other,HashableAny) and self.obj is other.obj

def _hashable( obj ):
  try:
    hash(obj)
  except:
    pass
  else:
    return obj
  return tuple( _hashable(o) for o in obj ) if isinstance( obj, tuple ) \
    else frozenset( _hashable(o) for o in obj ) if isinstance( obj, (set,frozenset) ) \
    else HashableArray( obj ) if isinstance( obj, numpy.ndarray ) \
    else HashableList( obj ) if isinstance( obj, list ) \
    else HashableDict( obj ) if isinstance( obj, dict ) \
    else HashableAny( obj )

def _keyfromargs( func, args, kwargs, offset=0 ):
  if getattr( func, '__self__', None ) is not None: # bound instancemethod
    offset += 1
  code = func.__code__
  names = code.co_varnames[offset+len(args):code.co_argcount]
  if names:
    kwargs = kwargs.copy()
    for name in names:
      try:
        val = kwargs.pop(name)
      except KeyError:
        index = names.index(name)-len(names)
        try:
          val = func.__defaults__[index]
        except Exception as e:
          raise TypeError( '%s missing mandatory argument %r' % ( func.__name__, name ) )
      args += val,
    assert not kwargs, '%s got invalid arguments: %s' % ( func.__name__, ', '.join(kwargs) )
  return tuple( _hashable(arg) for arg in args )


class CallDict( dict ):
  'very simple cache object'

  hit = 0

  def __call__( self, func, *args, **kwargs ):
    '''cache(func,*args,**kwargs):
    Execute func(*args,**kwargs) and cache the result.'''

    key = func, _keyfromargs( func, args, kwargs )
    value = self.get( key )
    if value is None:
      value = func( *args, **kwargs )
      self[ key ] = value
    else:
      self.hit += 1

    return value

  def summary( self ):
    return 'not used' if not self \
      else 'effectivity %d%% (%d hits, %d misses)' % ( (100*self.hit)/(self.hit+len(self)), self.hit, len(self) )


class ImmutableMeta( type ):
  def __init__( cls, *args, **kwargs ):
    type.__init__( cls, *args, **kwargs )
    cls.cache = weakref.WeakValueDictionary()
  def __call__( cls, *args, **kwargs ):
    key = _keyfromargs( cls.__init__, args, kwargs, 1 )
    try:
      self = cls.cache[key]
    except KeyError:
      self = type.__call__( cls, *args, **kwargs )
      cls.cache[key] = self
    return self

try: # for python 2/3 compatibility
  exec( 'class Immutable( object, metaclass=ImmutableMeta ): pass' )
except SyntaxError:
  class Immutable( object ):
    __metaclass__ = ImmutableMeta

def findargs( self ):
  for args, obj in self.__class__.cache.items():
    if obj is self:
      return args
  raise ValueError( 'object missing from cache' )

def immutable_str( self ):
  try:
    args = findargs(self)
  except ValueError:
    s = str( type(self) )
  else:
    s = '{}({})'.format( self.__class__.__name__, ','.join( str(arg) for arg in args ) )
  return s

Immutable.__reduce__ = lambda self: ( self.__class__, findargs(self) )
Immutable.__str__ = immutable_str

class FileCache( object ):
  'cache'

  def __init__( self, *args ):
    'constructor'

    if sys.version_info.major == 2:
      self.init1( args )
    else:
      self.init2( args )

  def init1( self, args ):
    import os, numpy
    self.myhash = hash( args )
    hexhash = hex( self.myhash )[2:]
    cachedir = core.getprop( 'cachedir', 'cache' )
    if not os.path.exists( cachedir ):
      os.makedirs( cachedir )
    path = os.path.join( cachedir, hexhash )
    allhash = numpy.array( [ hash(arg) for arg in args ], 'uint' )
    if not os.path.isfile( path ) or core.getprop( 'recache', False ):
      log.info( 'starting new cache:', hexhash )
      data = open( path, 'wb+' )
      allhash.tofile( data )
    else:
      log.info( 'continuing from cache:', hexhash )
      data = open( path, 'ab+' )
      checkhash = numpy.fromfile( data, 'uint', len(allhash) )
      assert all( checkhash == allhash ), 'hash clash'
    self.data = data

  def init2( self, args ):
    import os, numpy, hashlib, pickle
    serial = pickle.dumps( args, -1 )
    self.myhash = hash( serial )
    hexhash = hashlib.md5(serial).hexdigest()
    cachedir = core.getprop( 'cachedir', 'cache' )
    if not os.path.exists( cachedir ):
      os.makedirs( cachedir )
    path = os.path.join( cachedir, hexhash )
    if not os.path.isfile( path ) or core.getprop( 'recache', False ):
      log.info( 'starting new cache:', hexhash )
      data = open( path, 'wb+' )
      data.write( serial )
      data.flush()
    else:
      log.info( 'continuing from cache:', hexhash )
      data = open( path, 'ab+' )
      data.seek(0)
      recovered_args = pickle.load( data )
      assert recovered_args == args, 'hash clash'
    self.data = data

  def __call__( self, func, *args, **kwargs ):
    'call'

    try:
      import cPickle as pickle
    except ImportError:
      import pickle
    name = func.__name__ + ''.join( ' %s' % arg for arg in args ) + ''.join( ' %s=%s' % item for item in kwargs.items() )
    pos = self.data.tell()
    try:
      data = pickle.load( self.data )
    except EOFError:
      data = func( *args, **kwargs)
      self.data.seek( pos )
      pickle.dump( data, self.data, -1 )
      self.data.flush()
      msg = 'written to'
    else:
      msg = 'loaded from'
    log.info( msg, 'cache:', name, '[%db]' % (self.data.tell()-pos) )
    return data

  def __hash__( self ):
    return self.myhash


# vim:shiftwidth=2:foldmethod=indent:foldnestmax=2
