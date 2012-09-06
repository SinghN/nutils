from . import element, function, util, numpy, parallel, _

class Topology( set ):
  'topology base class'

  def __add__( self, other ):
    'add topologies'

    if self is other:
      return self
    assert self.ndims == other.ndims
    return UnstructuredTopology( set(self) | set(other), ndims=self.ndims )

  def __getitem__( self, item ):
    'subtopology'

    items = ( self.groups[it] for it in item.split( ',' ) )
    return sum( items, items.next() )

  def integrate( self, func, ischeme, coords=None, title=True ):
    'integrate'

    def makeindex( shape ):
      indices = []
      count = sum( isinstance(sh,function.DofAxis) for sh in shape )
      iaxis = 0
      for sh in shape:
        if isinstance(sh,function.DofAxis):
          index = sh
          if count > 1:
            reshape = [ numpy.newaxis ] * count
            reshape[iaxis] = slice(None)
            index = index[ tuple(reshape) ]
          iaxis += 1
        else:
          index = slice(None)
        indices.append( index )
      assert iaxis == count
      return function.Tuple(indices)

    if coords:
      J = coords.localgradient( self.ndims )
      cndims, = coords.shape
      if cndims == self.ndims:
        detJ = J.det( 0, 1 )
      elif self.ndims == 1:
        detJ = J[:,0].norm2( 0 )
      elif cndims == 3 and self.ndims == 2:
        detJ = function.Cross( J[:,0], J[:,1], axis=0 ).norm2( 0 )
      elif self.ndims == 0:
        detJ = 1.
      else:
        raise NotImplementedError, 'cannot compute determinant for %dx%d jacobian' % J.shape[:2]
    else:
      detJ = 1.

    topo = self if not title \
      else util.progressbar( self, title='integrating %d elements (nprocs=%d)' % ( len(self), parallel.nprocs ) if title is True else title )

    if isinstance( func, tuple ) or isinstance( func, function.ArrayFunc ) and len( func.shape ) == 2:
      # quickly implemented single array for now, needs to be extended for
      # multiple inputs. requires thinking of separating types for separate
      # arguments.

      import scipy.sparse

      if isinstance( func, tuple ):
        d = function.Tuple( [ function.Tuple( [ f, makeindex(f.shape) ] ) for f in func ] )
        shape = map( int, func[0].shape )
        for f in func[1:]:
          assert shape == map( int, f.shape )
      else:
        assert isinstance( func, function.ArrayFunc )
        d = function.Tuple( [ function.Tuple( [ func, makeindex(func.shape) ] ) ] )
        shape = func.shape

      idata = function.Tuple([ d, detJ ])
      indices = []
      values = []
      for elem in topo:
        xi = elem.eval(ischeme)
        datas, detj = idata(xi)
        weights = detj * xi.weights
        for data, index in datas:
          evalues = util.contract( data, weights )
          ndims = evalues.ndim
          eindices = numpy.empty( (ndims,) + evalues.shape )
          for i, n in enumerate( index ):
            eindices[i] = n
          values.append( evalues.ravel() )
          indices.append( eindices.reshape(ndims,-1) )

      v = numpy.hstack( values )
      ij = numpy.hstack( indices )
      A = scipy.sparse.csr_matrix( (v,ij), shape=shape )

    else:

      if isinstance( func, list ):
        A = [ parallel.shzeros( f.shape ) for f in func ]
        d = [ function.Tuple([ util.UsableArray(Ai), f, makeindex(f.shape) ]) for (Ai,f) in zip(A,func) ]
      else:
        A = parallel.shzeros( func.shape )
        d = [ function.Tuple([ util.UsableArray(A), func, makeindex(func.shape) ]) ]

      idata = function.Tuple([ detJ, function.Tuple(d) ])
      lock = parallel.Lock()
      for elem in parallel.pariter( topo ):
        xi = elem.eval(ischeme)
        detj, alldata = idata(xi)
        weights = detj * xi.weights
        with lock:
          for Ai, data, index in alldata:
            Ai[ index ] += util.contract( data, weights )

    return A

  def projection( self, fun, onto, **kwargs ):
    'project and return as function'

    weights = self.project( fun, onto, **kwargs )
    return onto.dot( weights )

  def project( self, fun, onto, coords=None, ischeme='gauss8', title=True, tol=1e-8, exact_boundaries=False, constrain=None ):
    'L2 projection of function onto function space'

    if exact_boundaries:
      assert constrain is None
      constrain = self.boundary.project( fun, onto, coords, ischeme=ischeme, title=None, tol=tol )
    elif constrain is None:
      constrain = util.NanVec( onto.shape[0] )
    else:
      assert isinstance( constrain, util.NanVec )
      assert constrain.shape == onto.shape[:1]

    if not isinstance( fun, function.Evaluable ):
      if callable( fun ):
        assert coords
        fun = function.UFunc( coords, fun )

    if len( onto.shape ) == 1:
      Afun = onto[:,_] * onto[_,:]
      bfun = onto * fun
    elif len( onto.shape ) == 2:
      Afun = ( onto[:,_,:] * onto[_,:,:] ).sum( 2 )
      bfun = ( onto * fun ).sum( 1 )
    else:
      raise Exception
    A, b = self.integrate( [Afun,bfun], coords=coords, ischeme=ischeme, title=title )

    zero = ( numpy.abs( A ) < tol ).all( axis=0 )
    constrain[zero] = 0
    if bfun == 0:
      u = constrain | 0
    else:
      u = util.solve( A, b, constrain )
    u[zero] = numpy.nan
    return u.view( util.NanVec )

class StructuredTopology( Topology ):
  'structured topology'

  def __init__( self, structure, periodic=None ):
    'constructor'

    assert isinstance( structure, numpy.ndarray )
    self.ndims = structure.ndim
    self.structure = structure
    self.periodic = periodic
    self.groups = {}

    Topology.__init__( self, structure.flat )

  def __len__( self ):
    'number of elements'

    return self.structure.size

  def __iter__( self ):
    'iterate'

    return self.structure.flat

  def __getitem__( self, item ):
    'subtopology'

    if isinstance( item, str ):
      return Topology.__getitem__( self, item )
    return StructuredTopology( self.structure[item] )

  @util.cacheprop
  def boundary( self ):
    'boundary'

    shape = numpy.asarray( self.structure.shape ) + 1
    nodes = numpy.arange( numpy.product(shape) ).reshape( shape )
    stdelem = element.PolyQuad( (2,)*(self.ndims-1) )

    boundaries = []
    for iedge in range( 2 * self.ndims ):
      idim = iedge // 2
      iside = iedge % 2
      s = [ slice(None,None,1-2*iside) ] * idim \
        + [ -iside ] \
        + [ slice(None,None,2*iside-1) ] * (self.ndims-idim-1)
      # TODO: check that this is correct for all dimensions; should match conventions in elem.edge
      belems = numpy.frompyfunc( lambda elem: elem.edge( iedge ), 1, 1 )( self.structure[s] )
      boundaries.append( StructuredTopology( belems ) )

    if self.ndims == 2:
      structure = numpy.concatenate([ boundaries[i].structure for i in [0,2,1,3] ])
      topo = StructuredTopology( structure, periodic=0 )
    else:
      allbelems = [ belem for boundary in boundaries for belem in boundary.structure.flat ]
      topo = UnstructuredTopology( allbelems, ndims=self.ndims-1 )

    topo.groups = dict( zip( ( 'left', 'right', 'bottom', 'top', 'front', 'back' ), boundaries ) )
    return topo

  @util.cachefunc
  def splinefunc( self, degree ):
    'spline from nodes'

    if isinstance( degree, int ):
      degree = ( degree, ) * self.ndims

    extractions = numpy.ones( (1,1,1), dtype=float )
    indices = numpy.array( 0 )
    slices = []
    for p, nelems in zip( degree, self.structure.shape ):
      n = min( nelems, 2*(p-1)-1 ) if self.periodic != len( slices ) else (2*(p-1)-1)
      ex = numpy.empty(( n, p, p ))
      ex[0] = numpy.eye( p )
      for i in range( 1, n ):
        ex[i] = numpy.eye( p )
        for j in range( 2, p ):
          for k in reversed( range( j, p ) ):
            alpha = 1. / min( 2+k-j, n-i+1 )
            ex[i-1,:,k] = alpha * ex[i-1,:,k] + (1-alpha) * ex[i-1,:,k-1]
          ex[i,-j-1:-1,-j-1] = ex[i-1,-j:,-1]
      extractions = util.reshape( extractions[:,_,:,_,:,_]
                                         * ex[_,:,_,:,_,:], 2, 2, 2 )
      if self.periodic == len( slices ):
        I = [p-2] * nelems
      else:
        I = range( n )
        if n < nelems:
          I[p-2:p-1] *= nelems - n + 1
      indices = indices[...,_] * n + I
      slices.append( [ slice(j,j+p) for j in range(nelems) ] )

    poly = element.PolyQuad( degree )
    stdelems = numpy.array( [ poly ] if all( p==2 for p in degree )
                       else [ element.ExtractionWrapper( poly, ex ) for ex in extractions ] )

    shape = [ n + p - 1 for n, p in zip( self.structure.shape, degree ) ]
    nodes_structure = numpy.arange( numpy.product(shape) ).reshape( shape )
    if self.periodic is None:
      dofcount = nodes_structure.size
    else:
      tmp = nodes_structure.swapaxes( 0, self.periodic )
      overlap = degree[self.periodic] - 1
      tmp[ -overlap: ] = tmp[ :overlap ]
      dofcount = tmp[ :-overlap ].size
    mapping = {}
    for item in numpy.broadcast( self.structure, *numpy.ix_(*slices) ):
      elem = item[0]
      S = item[1:]
      mapping[ elem ] = nodes_structure[S].ravel()
    shape = function.DofAxis( dofcount, mapping ),
    mapping = dict( ( elem, wrapper ) for elem, wrapper in numpy.broadcast( self.structure, stdelems[indices] ) )

    return function.Function( shape=shape, mapping=mapping )

  def linearfunc( self ):
    'linears'

    return self.splinefunc( degree=2 )

  def rectilinearfunc( self, gridnodes ):
    'rectilinear func'

    assert len( gridnodes ) == self.ndims
    nodes_structure = numpy.empty( map( len, gridnodes ) + [self.ndims] )
    for idim, inodes in enumerate( gridnodes ):
      shape = [1,] * self.ndims
      shape[idim] = -1
      nodes_structure[...,idim] = numpy.asarray( inodes ).reshape( shape )
    return self.linearfunc().dot( nodes_structure.reshape( -1, self.ndims ) )

  def refine( self, n ):
    'refine entire topology'

    if n == 1:
      return self

    N = n**self.ndims
    structure = numpy.array( [ elem.refined(n) for elem in self.structure.flat ] )
    structure = structure.reshape( self.structure.shape + (n,)*self.ndims )
    structure = structure.transpose( sum( [ ( i, self.ndims+i ) for i in range(self.ndims) ], () ) )
    structure = structure.reshape( [ self.structure.shape[i] * n for i in range(self.ndims) ] )

    return StructuredTopology( structure)

  def __str__( self ):
    'string representation'

    return '%s(%s)' % ( self.__class__.__name__, 'x'.join(map(str,self.structure.shape)) )

  def manifold( self, xi0, xi1 ):
    'create lower dimensional manifold in parent'

    assert self.ndims == 2
    scale = numpy.array( self.structure.shape )
    i0, j0 = numpy.asarray(xi0) * scale
    i1, j1 = numpy.asarray(xi1) * scale
    # j = A + B * i
    # j0 = A + B * i0
    # j1 = A + B * i1
    # B = (j1-j0) / float(i1-i0)
    # A = (j0*i1-j1*i0) / float(i1-i0)
    Ia = numpy.arange( int(i0), int(i1) ) + 1
    Ja = ( j0*i1 - j1*i0 + (j1-j0) * Ia ) / float(i1-i0)
    # i = C + D * j
    # i0 = C + D * j0
    # i1 = C + D * j1
    # D = (i1-i0) / float(j1-j0)
    # C = (i0*j1-i1*j0) / float(j1-j0)
    Jb = numpy.arange( int(j0), int(j1) ) + 1
    Ib = ( i0*j1 - i1*j0 + (i1-i0) * Jb ) / float(j1-j0)

    points = numpy.array( sorted( [(i0,j0),(i1,j1)] + zip(Ia,Ja) + zip(Ib,Jb) ) )
    keep = numpy.hstack( [ ( numpy.diff( points, axis=0 )**2 ).sum( axis=1 ) > 1e-9, [True] ] )
    points = points[keep]

    offsets = points - points.astype(int)
    transforms = numpy.diff( points, axis=0 )
    n, m = ( points[:-1] + .5 * transforms ).astype( int ).T
    pelems = self.structure[ n, m ]

    structure = []
    for pelem, offset, transform in zip( pelems, offsets, transforms ):
      trans = element.AffineTransformation( offset=offset, transform=transform[:,_] )
      elem = element.QuadElement( ndims=1, parent=(pelem,trans) )
      structure.append( elem )

    topo = StructuredTopology( numpy.asarray(structure) )

    weights = numpy.sqrt( ( ( points - points[0] )**2 ).sum( axis=1 ) / ( (i1-i0)**2 + (j1-j0)**2 ) )
    coords = topo.splinefunc( degree=2 ).dot( weights )

    return topo, coords

  def manifold2d( self, C0, C1, C2 ):
    'manifold 2d'

    np = 100
    n = numpy.arange( .5, np ) / np
    i = n[_,:,_]
    j = n[_,_,:]

    xyz = C0[:,_,_] + i * C1 + j * C2
    nxyz = int( xyz )
    fxyz = xyz - nxyz

    while len(n):
      ielem = nxyz[:,0]
      select = ( nxyz == ielem ).all( axis=0 )
      pelem = self.structure[ ielem ]

      trans = element.AffineTransformation( offset=offset, transform=transform[:,_] )
      elem = element.QuadElement( ndims=1, parent=(pelem,trans) )
      structure.append( elem )
    
class UnstructuredTopology( Topology ):
  'externally defined topology'

  groups = ()

  def __init__( self, elements, ndims, namedfuncs={} ):
    'constructor'

    self.namedfuncs = namedfuncs
    self.ndims = ndims

    Topology.__init__( self, elements )

  def splinefunc( self, degree ):
    'spline func'

    return self.namedfuncs[ 'spline%d' % degree ]

  def linearfunc( self ):
    'linear func'

    return self.splinefunc( degree=2 )

  def refine( self, n ):
    'refine entire topology'

    if n == 1:
      return self

    elements = []
    for elem in self:
      elements.extend( elem.refined(n) )
    return UnstructuredTopology( elements, ndims=self.ndims )

# vim:shiftwidth=2:foldmethod=indent:foldnestmax=2