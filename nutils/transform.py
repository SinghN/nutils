from . import cache, numeric, util, _


class Transformation( cache.Immutable ):
  __slots__ = 'fromdim', 'todim', 'sign'

  def __init__( self, todim, fromdim, sign ):
    assert todim == fromdim and sign == 0 or todim == fromdim + 1 and sign in (-1,1)
    self.todim = todim
    self.fromdim = fromdim
    self.sign = sign

  def __add__( self, offset ):
    offset = numeric.array( offset, float )
    assert offset.shape == (self.todim,)
    if numeric.equal( offset, 0 ).all():
      return self
    return Affine( offset, self )

  def __sub__( self, offset ):
    offset = numeric.asarray( offset, float )
    return self + (-offset)

  def __mul__( self, other ):
    return NotImplemented

  def __rmul__( self, other ):
    return NotImplemented

  def __repr__( self ):
    return '%s[%d<-%d](%s)' % ( self.__class__.__name__, self.todim, self.fromdim, self )


class Affine( Transformation ):
  __slots__ = 'offset', 'transform'

  def __init__( self, offset, transform ):
    Transformation.__init__( self, transform.todim, transform.fromdim, transform.sign )
    assert offset.shape == (self.todim,)
    self.offset = offset
    self.transform = transform

  @property
  def flipped( self ):
    return Affine( self.offset, self.transform.flipped )

  def apply( self, points, axis=-1 ):
    return self.offset + self.transform.apply( points, axis )

  def __add__( self, offset ):
    return self.transform + (self.offset+offset)

  def __mul__( self, other ):
    assert self.fromdim == other.todim
    if isinstance( other, Affine ):
      # self o other = self.offset + self.transform o ( other.offset + other.transform )
      return self.transform * other.transform + ( self.offset + self.transform.apply( other.offset ) )
    return self.transform * other + self.offset

  def __rmul__( self, other ):
    assert other.fromdim == self.todim
    # other o self = other o ( self.offset + self.transform )
    return other * self.transform + other.apply( self.offset )

  @property
  def det( self ):
    return self.transform.det

  @property
  def inv( self ):
    # y = self.offset + self.transform x <=> self.transform.inv y = self.tranform.inv self.offset + x
    inv = self.transform.inv
    return inv - inv.apply( self.offset )

  @property
  def matrix( self ):
    return self.transform.matrix

  def __repr__( self ):
    return '%s[%d<-%d](%s)' % ( self.transform.__class__.__name__, self.todim, self.fromdim, self )

  def __str__( self ):
    return '[%s] + %s' % ( ','.join( '%.2f'%v for v in self.offset ), self.transform )


class Linear( Transformation ):
  __slots__ = 'matrix',

  def __init__( self, matrix, sign=0 ):
    Transformation.__init__( self, matrix.shape[0], matrix.shape[1], sign )
    self.matrix = matrix

  def apply( self, points, axis=-1 ):
    assert points.shape[-1] == self.fromdim
    return numeric.dot( points, self.matrix.T, axis=axis )

  def __mul__( self, other ):
    assert self.fromdim == other.todim
    return Linear( numeric.dot( self.matrix, other.matrix ), self.sign + other.sign ) if isinstance( other, Linear ) \
      else Transformation.__mul__( self, other )

  @property
  def flipped( self ):
    return Linear( self.matrix, -self.sign )

  @property
  def det( self ):
    return numeric.det( self.matrix ) if self.fromdim == self.todim \
      else numeric.exterior( self.matrix ) * self.sign

  @property
  def inv( self ):
    assert self.fromdim == self.todim
    invmatrix = numeric.inv( self.matrix )
    return Linear( invmatrix )

  def __str__( self ):
    return ' + '.join( '[%s] x%d' % ( ','.join( '%.2f'%v for v in self.matrix[:,i] ), i ) for i in range(self.fromdim) )


class Slice( Linear ):
  __slots__ = 'slice',

  def __init__( self, fromdim, start, stop, step=1 ):
    self.slice = slice( start, stop, step )
    todim = len(range(start,stop,step))
    matrix = numeric.zeros( [todim,fromdim] )
    numeric.takediag( matrix[:,self.slice] )[:] = 1
    Linear.__init__( self, matrix )

  def apply( self, points, axis=-1 ):
    assert points.shape[-1] == self.fromdim
    return numeric.getitem( points, axis, self.slice )

  @staticmethod
  def nestslices( s1, s2 ):
    idx = range( s2.start, s2.stop, s2.step )[ s1 ]
    return idx[0], idx[-1] + ( 1 if idx[1] > idx[0] else -1 ), idx[1] - idx[0]

  def __mul__( self, other ):
    assert self.fromdim == other.todim
    return Slice( other.fromdim, *self.nestslices(self.slice,other.slice) ) if isinstance( other, Slice ) \
      else Scale( other.factors[self.slice] ) if isinstance( other, Scale ) \
      else Linear( other.matrix[self.slice] ) if isinstance( other, Linear ) \
      else Linear.__mul__( self, other )

  def __rmul__( self, other ):
    assert other.fromdim == self.todim
    return other * Linear( self.matrix ) # TODO make more efficient
      
  @property
  def det( self ):
    assert self.fromdim == self.todim
    return 1.

  @property
  def inv( self ):
    assert self.fromdim == self.todim
    return self

  def __str__( self ):
    return 'x[%d:%d:%d]' % ( self.slice.start, self.slice.stop, self.slice.step )


class Scale( Linear ):
  __slots__ = 'factors',

  def __init__( self, factors ):
    assert factors.ndim == 1
    self.factors = factors
    matrix = numeric.zeros( [factors.size,factors.size] )
    numeric.takediag(matrix)[:] = factors
    Linear.__init__( self, matrix )

  def apply( self, points, axis=-1 ):
    assert points.shape[axis] == self.fromdim
    assert axis == -1
    return points * self.factors

  def __mul__( self, other ):
    assert self.fromdim == other.todim
    return Scale( self.factors * other.factors ) if isinstance( other, Scale ) \
      else Linear( self.factors[:,_] * other.matrix, other.sign ) if isinstance( other, Linear ) \
      else Linear.__mul__( self, other )

  def __rmul__( self, other ):
    assert other.fromdim == self.todim
    return Linear( self.factors * other.matrix, other.sign ) if isinstance( other, Linear ) \
      else Linear.__rmul__( self, other )

  @property
  def det( self ):
    return numeric.prod( self.factors )

  @property
  def inv( self ):
    return Scale( numeric.reciprocal( self.factors ) )

  def __str__( self ):
    return '[%s] x' % ','.join( '%.2f' % v for v in self.factors )


class ScaleUniform( Scale ):
  __slots__ = 'factor',

  def __init__( self, ndims, factor ):
    self.factor = factor
    Scale.__init__( self, numeric.appendaxes( factor, ndims ) )

  def __mul__( self, other ):
    assert self.fromdim == other.todim
    return ScaleUniform( self.todim, self.factor * other.factor ) if isinstance( other, ScaleUniform ) \
      else Scale.__mul__( self, other )

  def apply( self, points, axis=-1 ):
    assert points.shape[axis] == self.fromdim
    return points * self.factor

  @property
  def det( self ):
    return self.factor**self.todim

  @property
  def inv( self ):
    return ScaleUniform( self.todim, 1./self.factor )

  def __str__( self ):
    return '%s x' % self.factor


class Identity( ScaleUniform ):
  __slots__ = ()

  def __init__( self, ndims ):
    ScaleUniform.__init__( self, ndims, 1. )

  def apply( self, points, axis=-1 ):
    return points

  def __mul__( self, other ):
    assert self.fromdim == other.todim
    return other

  def __rmul__( self, other ):
    assert other.fromdim == self.todim
    return other

  @property
  def det( self ):
    return 1.

  @property
  def inv( self ):
    return self

  def __str__( self ):
    return 'x'


class Root( Identity ):
  __slots__ = ()

  def __init__( self, ndims, token ):
    Identity.__init__( self, ndims )


class Point( Linear ):
  __slots__ = ()

  def __init__( self, sign ):
    Linear.__init__( self, numeric.zeros([1,0]), sign )

  def apply( self, points, axis=-1 ):
    shape = list( points.shape )
    assert shape[axis] == 0
    shape[axis] = 1
    return numeric.zeros( shape )

  def __str__( self ):
    return '0'


## UTILITY FUNCTIONS


def tensor( trans1, trans2 ):
  fromdim = trans1.fromdim + trans2.fromdim
  todim = trans1.todim + trans2.todim
  offset = numeric.zeros( todim )
  if isinstance( trans1, Affine ):
    offset[:trans1.todim] = trans1.offset
    trans1 = trans1.transform
  if isinstance( trans2, Affine ):
    offset[trans1.todim:] = trans2.offset
    trans2 = trans2.transform
  if isinstance( trans1, Identity ) and isinstance( trans2, Identity ):
    linear = Identidy( todim )
  elif isinstance( trans1, ScaleUniform ) and isinstance( trans2, ScaleUniform ) and trans1.factor == trans2.factor:
    linear = ScaleUniform( trans1.fromdim + trans2.fromdim, trans1.factor )
  elif isinstance( trans1, Scale ) and isinstance( trans2, Scale ):
    linear = Scale( numeric.concatenate([ trans1.factors, trans2.factors ]) )
  else:
    matrix = numeric.zeros( [todim,fromdim] )
    matrix[:trans1.todim,:trans1.fromdim] = trans1.matrix
    matrix[trans1.todim:,trans1.fromdim:] = trans2.matrix
    # TODO generalize
    if trans1.todim == 1 and trans1.fromdim == 0 and trans2.todim == trans2.fromdim == 1:
      sign = -trans1.sign
    elif trans1.todim == trans1.fromdim == 1 and trans2.todim == 1 and trans2.fromdim == 0:
      sign = trans2.sign
    elif trans1.todim == 1 and trans1.fromdim == 0 and trans2.todim == trans2.fromdim == 2:
      sign = trans1.sign
    elif trans1.todim == trans1.fromdim == 1 and trans2.todim == 2 and trans2.fromdim == 1:
      sign = trans2.sign
    else:
      raise NotImplementedError
    linear = Linear( matrix, sign )
  return linear + offset

def split_linear_offset( trans ):
  if isinstance( trans, Affine ):
    return trans.transform, trans.offset
  return trans, numeric.zeros( trans.todim )

def canonical( trans ):
  # keep at lowest ndims possible
  for i in reversed( range(1,len(trans)) ):
    if trans[i].fromdim == trans[0].todim:
      break
    while i < len(trans):
      scale, updim = trans[i-1:i+1]
      if isinstance( scale, Root ) or updim.todim != updim.fromdim + 1:
        break
      _scale, c = split_linear_offset(scale)
      if not isinstance( _scale, ScaleUniform ):
        break
      _updim, b = split_linear_offset(updim)
      A = _updim.matrix
      AA = numeric.dot( A.T, A )
      Av = numeric.dot( A.T, c + (_scale.factor-1) * b )
      d = numeric.solve( AA, Av )
      newscale = ScaleUniform( updim.fromdim, _scale.factor ) + d
      assert updim * newscale == scale * updim
      trans = trans[:i-1] + (updim,newscale) + trans[i+1:]
      i += 1
  return trans

def prioritize( trans, ndims ):
  # assuming canonical, move up to ndims asap, stay at ndims alap
  for i in range(1,len(trans)):
    while i and trans[i].todim < ndims:
      scale, updim = trans[i-1:i+1]
      if updim.todim != updim.fromdim + 1:
        break
      _scale, d = split_linear_offset(scale)
      if not isinstance( _scale, ScaleUniform ):
        break
      _updim, b = split_linear_offset(updim)
      c = _updim.apply( d ) + (1-_scale.factor) * b
      newscale = ScaleUniform( updim.todim, _scale.factor ) + c
      assert newscale * updim == updim * scale
      trans = trans[:i-1] + (newscale,updim) + trans[i+1:]
      i -= 1
  return trans
