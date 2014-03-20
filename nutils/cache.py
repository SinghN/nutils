from . import core, log
import os, weakref

_property = property
def property( f ):
  def cache_property_wrapper( self, f=f ):
    try:
      value = self.__dict__[f.func_name]
    except KeyError:
      value = f( self )
      self.__dict__[f.func_name] = value
    return value
  assert not cache_property_wrapper.__closure__
  return _property(cache_property_wrapper)

class PropertyList(object):
  __slots__ = 'items', 'func', 'obj'
  def __init__( self, items, func, obj ):
    self.items = items
    self.func = func
    self.obj = obj
  def append( self ):
    value = self.func(self.obj,len(self.items))
    self.items.append( value )
    return value
  def __iter__( self ):
    for item in self.items:
      yield item
    while True:
      yield self.append()
  def __getitem__( self, item ):
    assert isinstance(item,int) and item >= 0
    while item >= len(self.items):
      self.append()
    return self.items[item]

def propertylist( f ):
  def cache_propertylist_wrapper( self, f=f ):
    items = self.__dict__.setdefault( f.func_name, [] )
    return PropertyList( items, f, self )
  assert not cache_propertylist_wrapper.__closure__
  return _property(cache_propertylist_wrapper)

class CallDict( dict ):
  'very simple cache object'

  hit = 0

  def __call__( self, *key ):
    'cache(func,*args): execute func(args) and cache result'
    value = self.get( key )
    if value is None:
      value = key[0]( *key[1:] )
      self[ key ] = value
    else:
      self.hit += 1
    return value

  def summary( self ):
    return 'not used' if not self \
      else 'effectivity %d%% (%d hits, %d misses)' % ( (100*self.hit)/(self.hit+len(self)), self.hit, len(self) )

class Immutable( object ):
  'weakly cache object instances based on init args'

  __slots__ = '__weakref__',

  class __metaclass__( type ):
    def __init__( cls, *args, **kwargs ):
      type.__init__( cls, *args, **kwargs )
      cls.cache = weakref.WeakValueDictionary()
    def __call__( cls, *args, **kwargs ):
      code = cls.__init__.func_code
      names = code.co_varnames[len(args)+1:code.co_argcount]
      for name in names:
        try:
          val = kwargs.pop(name)
        except KeyError:
          index = names.index(name)-len(names)
          try:
            val = cls.__init__.func_defaults[index]
          except Exception as e:
            raise TypeError, '%s.__init__ missing mandatory argument %r' % ( cls.__name__, name )
        args += val,
      assert not kwargs, '%s.__init__ got invalid arguments: %s' % ( cls.__name__, ', '.join(kwargs) )
      try:
        self = cls.cache[args]
      except KeyError:
        self = type.__call__( cls, *args )
        cls.cache[args] = self
      return self

class FileCache( object ):
  'cache'

  def __init__( self, *args ):
    'constructor'

    import hashlib
    strhash = ','.join( str(arg) for arg in args )
    md5hash = hashlib.md5( strhash ).hexdigest()
    log.info( 'using cache:', md5hash )
    cachedir = core.prop( 'cachedir', 'cache' )
    if not os.path.exists( cachedir ):
      os.makedirs( cachedir )
    path = os.path.join( cachedir, md5hash )
    self.data = file( path, 'ab+' if not core.prop( 'recache', False ) else 'wb+' )

  def __call__( self, func, *args, **kwargs ):
    'call'

    import cPickle
    name = func.__name__ + ''.join( ' %s' % arg for arg in args ) + ''.join( ' %s=%s' % item for item in kwargs.iteritems() )
    pos = self.data.tell()
    try:
      data = cPickle.load( self.data )
    except EOFError:
      data = func( *args, **kwargs)
      self.data.seek( pos )
      cPickle.dump( data, self.data, -1 )
      msg = 'written to'
    else:
      msg = 'loaded from'
    log.info( msg, 'cache:', name, '[%db]' % (self.data.tell()-pos) )
    return data


# vim:shiftwidth=2:foldmethod=indent:foldnestmax=2
