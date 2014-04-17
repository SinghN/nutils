from . import element, function, util, cache, parallel, matrix, log, numeric, transform, pointset, _
import warnings, itertools

class ElemMap( dict ):
  'dictionary-like element mapping'

  def __init__( self, mapping, ndims ):
    'constructor'

    self.ndims = ndims
    dict.__init__( self, mapping )

  def __eq__( self, other ):
    'test equal'

    return self is other

  def __str__( self ):
    'string representation'

    return 'ElemMap(#%d,%dD)' % ( len(self), self.ndims )

class Topology( object ):

  def __init__( self, ndims, elements ):
    self.ndims = ndims

    self.estack = numeric.empty( (len(elements),2), dtype=object )
    for i, (trans,head) in enumerate( elements ):
      self.estack[i,0] = trans
      self.estack[i,1] = head

    assert numeric.greater( self.estack[1:,0], self.estack[:-1,0] ).all() # check sorted

  @cache.property
  def elements( self ):
    return numeric.asobjvec( map( tuple, self.estack ) )

  @property
  def elements_nohead( self ):
    return self.estack[:,0]

  def __getitem__( self, item ):
    return self.elements[ item ]

  def index( self, elements ):
    if isinstance( elements, Topology ):
      elements = elements.elements
    return numeric.findsorted( self.elements, elements )

  def _set( self, other, op ):
    assert self.ndims == other.ndims
    return Topology( self.ndims, op( self.elements, other.elements ) )
    
  def __and__( self, other ): return self._set( other, numeric.intersect1d )
  def __or__ ( self, other ): return self._set( other, numeric.union1d     )
  def __xor__( self, other ): return self._set( other, numeric.setxor1d    )
  def __sub__( self, other ): return self._set( other, numeric.setdiff1d   )

  def __iter__( self ):
    return iter( self.elements )

  def __len__( self ):
    return len( self.elements )

  @log.title
  def build_graph( self, func ):
    'get matrix sparsity'

    nrows, ncols = func.shape
    graph = [ [] for irow in range(nrows) ]
    IJ = function.Tuple([ function.Tuple(ind) for f, ind in function.blocks( func ) ]).compiled()

    __logger__ = log.iter( 'elem', self )
    for elem in __logger__:
      for I, J in IJ.eval( elem, None ):
        for i in I:
          graph[i].append(J)

    __logger__ = log.enumerate( 'dof', graph )
    for irow, g in __logger__:
      # release memory as we go
      if g:
        graph[irow] = numeric.unique( numeric.concatenate( g ) )

    return graph

  @log.title
  def integrate( self, funcs, ischeme, geometry=None, iweights=None, force_dense=False ):
    'integrate'

    single_arg = not isinstance(funcs,(list,tuple))
    if single_arg:
      funcs = funcs,

    if iweights is None:
      assert geometry is not None, 'conflicting arguments geometry and iweights'
      iweights = function.iwscale( geometry, self.ndims )
    else:
      assert geometry is None, 'conflicting arguments geometry and iweights'
    assert iweights.ndim == 0

    integrands = []
    retvals = []
    for ifunc, func in enumerate( funcs ):
      func = function.asarray( func )
      lock = parallel.Lock()
      if function._isfunc( func ):
        array = parallel.shzeros( func.shape, dtype=float ) if func.ndim != 2 \
           else matrix.DenseMatrix( func.shape ) if force_dense \
           else matrix.SparseMatrix( self.build_graph(func), func.shape[1] )
        for f, ind in function.blocks( func ):
          integrands.append( function.Tuple([ ifunc, lock, function.Tuple(ind), function.elemint( f, iweights ) ]) )
      else:
        array = parallel.shzeros( func.shape, dtype=float )
        if not function._iszero( func ):
          integrands.append( function.Tuple([ ifunc, lock, (), function.elemint( func, iweights ) ]) )
      retvals.append( array )
    idata = function.Tuple( integrands ).compiled()

    points = pointset.aspointset( ischeme )
    __logger__ = log.iter( 'elem', self )
    for elem in parallel.pariter( __logger__ ):
      for ifunc, lock, index, data in idata.eval( elem, points ):
        retval = retvals[ifunc]
        with lock:
          retval[index] += data

    log.debug( 'cache', idata.cache.summary() )
    log.info( 'created', ', '.join( '%s(%s)' % ( retval.__class__.__name__, ','.join(map(str,retval.shape)) ) for retval in retvals ) )
    if single_arg:
      retvals, = retvals

    return retvals

  def refine( self, n ):
    'refine entire topology n times'
    return self if n <= 0 else self.refined.refine( n-1 )

  def projection( self, fun, onto, geometry, **kwargs ):
    'project and return as function'

    weights = self.project( fun, onto, geometry, **kwargs )
    return onto.dot( weights )

  @log.title
  def project( self, fun, onto, geometry, tol=0, ischeme=None, droptol=1e-8, exact_boundaries=False, constrain=None, verify=None, maxiter=0, ptype='lsqr' ):
    'L2 projection of function onto function space'

    log.debug( 'projection type:', ptype )

    points = pointset.aspointset( ischeme )
    if exact_boundaries:
      assert constrain is None
      constrain = self.boundary.project( fun, onto, geometry, title='boundaries', ischeme=points, tol=tol, droptol=droptol, ptype=ptype )
    elif constrain is None:
      constrain = util.NanVec( onto.shape[0] )
    else:
      assert isinstance( constrain, util.NanVec )
      assert constrain.shape == onto.shape[:1]

    if ptype == 'lsqr':
      assert points is not None, 'please specify an integration scheme for lsqr-projection'
      if len( onto.shape ) == 1:
        Afun = function.outer( onto )
        bfun = onto * fun
      elif len( onto.shape ) == 2:
        Afun = function.outer( onto ).sum( 2 )
        bfun = function.sum( onto * fun )
      else:
        raise Exception
      A, b = self.integrate( [Afun,bfun], geometry=geometry, ischeme=points, title='building system' )
      N = A.rowsupp(droptol)
      if numeric.equal( b, 0 ).all():
        constrain[~constrain.where&N] = 0
      else:
        solvecons = constrain.copy()
        solvecons[~(constrain.where|N)] = 0
        u = A.solve( b, solvecons, tol=tol, symmetric=True, maxiter=maxiter )
        constrain[N] = u[N]

    elif ptype == 'convolute':
      assert points is not None, 'please specify an integration scheme for convolute-projection'
      if len( onto.shape ) == 1:
        ufun = onto * fun
        afun = onto
      elif len( onto.shape ) == 2:
        ufun = function.sum( onto * fun )
        afun = function.norm2( onto )
      else:
        raise Exception
      u, scale = self.integrate( [ ufun, afun ], geometry=geometry, ischeme=points )
      N = ~constrain.where & ( scale > droptol )
      constrain[N] = u[N] / scale[N]

    elif ptype == 'nodal':

      ## data = function.Tuple([ fun, onto ])
      ## F = W = 0
      ## for elem in self:
      ##   f, w = data( elem, 'bezier2' )
      ##   W += w.sum( axis=-1 ).sum( axis=0 )
      ##   F += numeric.contract( f[:,_,:], w, axis=[0,2] )
      ## I = (W!=0)

      F = numeric.zeros( onto.shape[0] )
      W = numeric.zeros( onto.shape[0] )
      I = numeric.zeros( onto.shape[0], dtype=bool )
      fun = function.asarray( fun )
      data = function.Tuple( function.Tuple([ fun, f, function.Tuple(ind) ]) for f, ind in function.blocks( onto ) )
      for elem in self:
        for f, w, ind in data( elem, 'bezier2' ):
          w = w.swapaxes(0,1) # -> dof axis, point axis, ...
          wf = w * f[ (slice(None),)+numeric.ix_(*ind[1:]) ]
          W[ind[0]] += w.reshape(w.shape[0],-1).sum(1)
          F[ind[0]] += wf.reshape(w.shape[0],-1).sum(1)
          I[ind[0]] = True

      I[constrain.where] = False
      constrain[I] = F[I] / W[I]

    else:
      raise Exception, 'invalid projection %r' % ptype

    errfun2 = ( onto.dot( constrain | 0 ) - fun )**2
    if errfun2.ndim == 1:
      errfun2 = errfun2.sum()
    error2, area = self.integrate( [ errfun2, 1 ], geometry=geometry, ischeme=points or 'gauss2' )
    avg_error = numeric.sqrt(error2) / area

    numcons = constrain.where.sum()
    if verify is not None:
      assert numcons == verify, 'number of constraints does not meet expectation: %d != %d' % ( numcons, verify )

    log.info( 'constrained %d/%d dofs, error %.2e/area' % ( numcons, constrain.size, avg_error ) )

    return constrain

  @log.title
  def elem_eval( self, funcs, ischeme, separate=False ):
    'element-wise evaluation'

    single_arg = not isinstance(funcs,(tuple,list))
    if single_arg:
      funcs = funcs,

    slices = []
    pointshape = function.PointShape().compiled()
    npoints = 0
    separators = []
    points = pointset.aspointset( ischeme )
    __logger__ = log.iter( 'elem', self )
    for elem in __logger__:
      np, = pointshape.eval( elem, points )
      slices.append( slice(npoints,npoints+np) )
      npoints += np
      if separate:
        separators.append( npoints )
        npoints += 1
    if separate:
      separators = numeric.array( separators[:-1], dtype=int )
      npoints -= 1

    retvals = []
    idata = []
    for ifunc, func in enumerate( funcs ):
      func = function.asarray( func )
      retval = parallel.shzeros( (npoints,)+func.shape, dtype=func.dtype )
      if separate:
        retval[separators] = numeric.nan
      if function._isfunc( func ):
        for f, ind in function.blocks( func ):
          idata.append( function.Tuple( [ ifunc, function.Tuple(ind), f ] ) )
      else:
        idata.append( function.Tuple( [ ifunc, (), func ] ) )
      retvals.append( retval )
    idata = function.Tuple( idata ).compiled()

    __logger__ = log.enumerate( 'elem', self )
    for ielem, elem in parallel.pariter( __logger__ ):
      s = slices[ielem],
      for ifunc, index, data in idata.eval( elem, points ):
        retvals[ifunc][s+index] = data

    log.info( 'cache', idata.cache.summary() )
    log.info( 'created', ', '.join( '%s(%s)' % ( retval.__class__.__name__, ','.join(map(str,retval.shape)) ) for retval in retvals ) )
    if single_arg:
      retvals, = retvals

    return retvals

  @log.title
  def trim( self, levelset, maxrefine=0, minrefine=0 ):
    'trim element along levelset'

    levelset = function.ascompiled( levelset )
    pos = []
    neg = []
    __logger__ = log.enumerate( 'elem', self )
    for ielem, (trans,head) in __logger__:
      p, n = head.trim( levelset=(trans+(levelset,)), maxrefine=maxrefine, minrefine=minrefine )
      if p: pos.append(( trans,p ))
      if n: neg.append(( trans,n ))
    # pos, nul, neg are sorted
    postopo = TrimmedTopology( self, elements=pos, iface=nul )
    negtopo = TrimmedTopology( self, elements=neg, iface=invnul )
    raise NotImplementedError
    return postopo, negtopo

  @cache.property
  def simplex( self ):
    simplices = numeric.asobjvec( (etrans+(strans,),shead) for etrans, ehead in self for strans, shead in ehead.simplices )
    simplices.sort()
    return Topology( ndims=self.ndims, elements=simplices )

  def get_simplices( self ):
    warnings.warn( '.getsimplices() is replaced by .simplex', DeprecationWarning )
    return self.simplex

  @cache.property
  def refined( self ):
    return RefinedTopology( self )

  def refined_by( self, refine ):
    'create refined space by refining dofs in existing one'

    elements = list( self )
    for elem in refine:
      index = numeric.bisect( elements, elem ) + 1
      match = elements[index]
      assert match[:len(elem)] == elem
      if len(match) == len(elem) + 1: # elem is top level -> refine
        elements[index:index+1] = sorted( match[:-1] + child for child in match[-1].children )
    return HierarchicalTopology( self, elements )

class UnstructuredTopology( Topology ):

  def __init__( self, ndims, elements, groups={}, boundary=None ):
    elements = numeric.sort( numeric.asobjvec( elements ) )
    Topology.__init__( self, ndims, elements )
    assert all( isinstance( topo, Topology ) and topo.ndims == ndims for topo in groups.values() )
    self.groups = groups
    if boundary is not None:
      assert isinstance( boundary, Topology ) and boundary.ndims == ndims-1
    self.boundary = boundary

  def __getitem__( self, item ):
    if isinstance( item, str ):
      return eval( item.replace(',','|'), self.groups )
    return Topology.__getitem__( self, item )

class StructuredTopology( Topology ):

  def __init__( self, structure, periodic=() ):
    nNone = numeric.equal(structure,None).sum()
    indices = structure.argsort(axis=None)
    assert numeric.equal( structure.flat[indices[:nNone]], None ).all()
    self.istructure = numeric.empty( structure.shape, dtype=int )
    self.istructure.flat[indices] = numeric.maximum( numeric.arange( len(indices) )-nNone, -1 )
    self.periodic = tuple(periodic)
    self.groups = {}
    Topology.__init__( self, ndims=structure.ndim, elements=structure.flat[indices[nNone:]] )
    assert numeric.equal( structure, self.structure ).all() # TODO fix '=='

  @property
  def structure( self ):
    return self.structure_like( self.elements )

  def structure_like( self, elements ): 
    structure = numeric.empty( self.istructure.shape, dtype=object )
    select = numeric.greater_equal( self.istructure.flat, 0 )
    structure.flat[select] = elements[self.istructure.flat[select]]
    return structure

  @cache.property
  def boundary( self ):
    'boundary'

    shape = numeric.asarray( self.istructure.shape ) + 1
    vertices = numeric.arange( numeric.product(shape) ).reshape( shape )

    boundaries = []
    for iedge in range( 2 * self.ndims ):
      idim = iedge // 2
      iside = -1 if iedge % 2 == 0 else 0
      ielems = numeric.getitem( self.istructure[...,_], axis=idim, item=iside ) # add axis to keep an array even if ndims=1
      belems = numeric.empty( ielems.shape[:-1], dtype=object )
      for index, ielem in numeric.enumerate_nd( ielems ):
        if ielem >= 0:
          etrans, ehead = self.elements[ielem]
          edgetrans, edgehead = ehead.edges[iedge]
          etrans += edgetrans,
          belems[ index[:-1] ] = transform.canonical( etrans ), edgehead
      periodic = [ d - (d>idim) for d in self.periodic if d != idim ] # TODO check that dimensions are correct for ndim > 2
      boundaries.append( StructuredTopology( belems, periodic=periodic ) if self.ndims > 1
                    else Topology( self.ndims-1, list(belems.flat) ) )
    groups = dict( zip( ( 'right', 'left', 'top', 'bottom', 'back', 'front' ), boundaries ) )

    allbelems = [ belem for boundary in boundaries for belem in boundary ]
    return UnstructuredTopology( elements=allbelems, ndims=self.ndims-1, groups=groups )

  def splinefunc( self, degree, neumann=(), periodic=None, closed=False, removedofs=None ):
    'spline from vertices'

    if periodic is None:
      periodic = self.periodic

    if isinstance( degree, int ):
      degree = ( degree, ) * self.ndims

    if removedofs == None:
      removedofs = [None] * self.ndims
    else:
      assert len(removedofs) == self.ndims

    vertex_structure = numeric.array( 0 )
    dofcount = 1
    slices = []

    for idim in range( self.ndims ):
      periodic_i = idim in periodic
      n = self.istructure.shape[idim]
      p = degree[idim]

      if closed == False:
        neumann_i = (idim*2 in neumann and 1) | (idim*2+1 in neumann and 2)
        stdelems_i = element.PolyLine.spline( degree=p, nelems=n, periodic=periodic_i, neumann=neumann_i )
      elif closed == True:
        assert periodic==(), 'Periodic option not allowed for closed spline'
        assert neumann ==(), 'Neumann option not allowed for closed spline'
        stdelems_i = element.PolyLine.spline( degree=p, nelems=n, periodic=True )

      stdelems = stdelems[...,_] * stdelems_i if idim else stdelems_i

      nd = n + p
      numbers = numeric.arange( nd )
      if periodic_i and p > 0:
        overlap = p
        numbers[ -overlap: ] = numbers[ :overlap ]
        nd -= overlap
      remove = removedofs[idim]
      if remove is None:
        vertex_structure = vertex_structure[...,_] * nd + numbers
      else:
        mask = numeric.zeros( nd, dtype=bool )
        mask[numeric.array(remove)] = True
        nd -= mask.sum()
        numbers -= mask.cumsum()
        vertex_structure = vertex_structure[...,_] * nd + numbers
        vertex_structure[...,mask] = -1
      dofcount *= nd
      slices.append( [ slice(i,i+p+1) for i in range(n) ] )

    dofmap = numeric.empty( len(self), dtype=object )
    funcmap = numeric.empty( len(self), dtype=object )
    hasnone = False
    for item in numeric.broadcast( self.istructure, stdelems, *numeric.ix_(*slices) ):
      ielem = item[0]
      if ielem < 0:
        hasnone = True
      else:  
        elem = self.elements[ ielem ][:-1]
        std = item[1]
        S = item[2:]
        dofs = vertex_structure[S].ravel()
        mask = numeric.greater_equal( dofs, 0 )
        if mask.all():
          dofmap[ ielem ] = dofs
          func = std
        elif mask.any():
          dofmap[ ielem ] = dofs[mask]
          func = std, mask
        etrans, ehead = self.elements[ielem]
        funcmap[ ielem ] = (None,) * len(etrans) + (func,)

    if hasnone:
      raise NotImplementedError
      touched = numeric.zeros( dofcount, dtype=bool )
      for dofs in dofmap.itervalues():
        touched[ dofs ] = True
      renumber = touched.cumsum()
      dofcount = int(renumber[-1])
      dofmap = dict( ( elem, renumber[dofs]-1 ) for elem, dofs in dofmap.iteritems() )

    return function.function( self.elements_nohead, funcmap, dofmap, dofcount, self.ndims )

  def stdfunc( self, degree ):
    'spline from vertices'

    if isinstance( degree, int ):
      degree = ( degree, ) * self.ndims

    vertex_structure = numeric.array( 0 )
    dofcount = 1
    slices = []

    for idim in range( self.ndims ):
      n = self.istructure.shape[idim]
      p = degree[idim]

      nd = n * p + 1
      numbers = numeric.arange( nd )
      if idim in self.periodic:
        numbers[-1] = numbers[0]
        nd -= 1
      vertex_structure = vertex_structure[...,_] * nd + numbers
      dofcount *= nd
      slices.append( [ slice(p*i,p*i+p+1) for i in range(n) ] )

    dofmap = numeric.empty( len(self), dtype=object )
    hasnone = False
    for item in numeric.broadcast( self.istructure, *numeric.ix_(*slices) ):
      ielem = item[0]
      if ielem < 0:
        hasnone = True
      else:
        elem = self.elements[ ielem ]
        S = item[1:]
        dofmap[ielem] = vertex_structure[S].ravel()

    if hasnone:
      raise NotImplementedError
      touched = numeric.zeros( dofcount, dtype=bool )
      for dofs in dofmap.itervalues():
        touched[ dofs ] = True
      renumber = touched.cumsum()
      dofcount = int(renumber[-1])
      dofmap = dict( ( elem, renumber[dofs]-1 ) for elem, dofs in dofmap.iteritems() )

    std = util.product( element.PolyLine( element.PolyLine.bernstein_poly( d ) ) for d in degree )
    funcmap = numeric.asobjvec( (None,) * len(elem[:-1]) + (std,) for elem in self )

    return function.function( self.elements_nohead, funcmap, dofmap, dofcount, self.ndims )

  @cache.property
  def refined( self ):
    'refine entire topology'

    transforms = tuple( trans for trans, head in ( element.Simplex(1)**self.ndims ).children )

    structure = numeric.empty( self.istructure.shape + (2**self.ndims,), dtype=object )
    for index, ielem in numeric.enumerate_nd( self.istructure ):
      if ielem >= 0:
        trans, head = self.elements[ielem]
        for ctrans, chead in head.children:
          cindex = transforms.index( ctrans )
          structure[index][cindex] = trans+(ctrans,), chead
    structure = structure.reshape( self.istructure.shape + (2,)*self.ndims )
    structure = structure.transpose( sum( [ ( i, self.ndims+i ) for i in range(self.ndims) ], () ) )
    structure = structure.reshape([ sh * 2 for sh in self.istructure.shape ])
    refined = StructuredTopology( structure )
    refined.groups = { key: group.refined for key, group in self.groups.items() }
    return refined

class HierarchicalTopology( Topology ):
  'collection of nested topology elments'

  def __init__( self, basetopo, elements ):
    'constructor'

    if isinstance( basetopo, HierarchicalTopology ):
      basetopo = basetopo.basetopo
    self.basetopo = basetopo
    self.maxrefine = max( len(elem) for elem in elements ) \
                   - min( len(elem) for elem in self.basetopo )
    Topology.__init__( self, basetopo.ndims, elements )

  @log.title
  def _funcspace( self, mkspace ):

    collect = {}
    ndofs = 0 # total number of dofs of new function object
    remaining = len(self) # element count down (know when to stop)
  
    topo = self.basetopo # topology to examine in next level refinement
    for irefine in range( self.maxrefine+1 ):

      funcsp = mkspace( topo ) # shape functions for level irefine
      supported = numeric.ones( funcsp.shape[0], dtype=bool ) # True if dof is fully contained in self or parents
      touchtopo = numeric.zeros( funcsp.shape[0], dtype=bool ) # True if dof touches at least one elem in self
      myelems = [] # all top-level or parent elements in level irefine

      for elem, idofs, stds in function._unpack( funcsp ):
        index = numeric.bisect( self.elements_nohead, elem )
        found = self.elements_nohead[index]
        if found == elem:
          remaining -= 1
          touchtopo[idofs] = True
          myelems.append(( elem, idofs, stds ))
        elif elem[:len(found)] == found:
          supported[idofs] = False
        else:
          myelems.append(( elem, idofs, stds ))
  
      keep = numeric.logical_and( supported, touchtopo ) # THE refinement law

      for elem, idofs, stds in myelems: # loop over all top-level or parent elements in level irefine
        assert all( std is None for std in stds[:-1] )
        std = stds[-1]
        assert isinstance(std,element.StdElem)
        if irefine:
          olddofs, oldstds = collect[ elem[:-1] ] # dofs, stds of all underlying 'broader' shapes
          assert len(stds) == len(oldstds) + 1
        else:
          olddofs = numeric.zeros( [0], dtype=int )
          oldstds = stds[:-1]

        mykeep = keep[idofs]
        newstds = oldstds \
                + ( std if mykeep.all() # use all shapes from this level
               else (std,mykeep) if mykeep.any() # use some shapes from this level
               else None, )

        newdofs = olddofs if not mykeep.any() \
             else numeric.hstack([ olddofs,
          [ ndofs + keep[:idof].sum() for idof in idofs if keep[idof] ] ]) # new dof numbers

        collect[ elem ] = newdofs, newstds # add result to IEN mapping of new function object
  
      ndofs += int( keep.sum() ) # update total number of dofs
      if not remaining:
        break
      topo = topo.refined # proceed to next level
  
    else:

      raise Exception, 'elements remaining after %d iterations' % self.maxrefine

    dofmap, funcmap = zip( *[ collect[elem] for elem in self.elements_nohead ] )
    return function.function( self.elements_nohead, funcmap, dofmap, ndofs, self.ndims )

  def stdfunc( self, *args, **kwargs ):
    return self._funcspace( lambda topo: topo.stdfunc( *args, **kwargs ) )

  def splinefunc( self, *args, **kwargs ):
    return self._funcspace( lambda topo: topo.splinefunc( *args, **kwargs ) )

class RefinedTopology( Topology ):
  'refinement'

  def __init__( self, basetopo ):
    self.basetopo = basetopo
    elements = numeric.asobjvec( (etrans+(ctrans,),chead) for etrans, ehead in basetopo for ctrans, chead in ehead.children )
    elements.sort()
    Topology.__init__( self, basetopo.ndims, elements )

  def __getitem__( self, key ):
    return self.basetopo[key].refined

  @property
  def boundary( self ):
    return self.basetopo.boundary.refined
    
class TrimmedTopology( Topology ):
  'trimmed'

  def __init__( self, basetopo, elements, iface=None ):
    self.basetopo = basetopo
    self.iface = iface
    Topology.__init__( self, basetopo.ndims, elements )

  @property
  def boundary( self ):
    belems = []#list( self.iface )
    for trans, head in self.basetopo.boundary:
      index = numeric.bisect( self.elements_nohead, trans[:-1] )
      if index < 0:
        continue
      ptrans, phead = self.elements[ index ]
      if ptrans != trans[:-1]:
        continue
      ehead = phead.edgedict.get( trans[-1] )
      if ehead is None:
        continue
      belems.append( (trans,ehead) )
    belems.sort()
    return TrimmedTopology( self.basetopo.boundary, belems )

  def __getitem__( self, key ):
    if key == 'trim':
      # all elements in self that are not in basetopo
      indices = numeric.bisect_sorted( self.elements_nohead, self.basetopo.elements_nohead, matching=True )
      select = numeric.ones( len(self), dtype=bool )
      select[indices] = False
      assert select.any(), 'no trimmed elements found in trim group'
      return Topology( self.ndims, self.elements[select] )
    else:
      # all elements in basetopo[key] that are also in self
      keytopo = self.basetopo[ key ]
      indices = numeric.bisect_sorted( self.elements_nohead, keytopo.elements_nohead, matching=True )
      elements = self.elements[indices]
      if numeric.equal( elements, keytopo.elements ).all():
        return keytopo
      assert elements, 'no trimmed elements found in %s group' % key
      return TrimmedTopology( keytopo, elements )

  def splinefunc( self, *args, **kwargs ):
    return self.basetopo.splinefunc( *args, **kwargs )

  


## OLD

class _Topology( object ):
  'topology base class'

  def __init__( self, ndims ):
    'constructor'

    self.ndims = ndims

  def stdfunc( self, degree ):
    'spline from vertices'

    assert degree == 1 # for now!
    dofmap = { n: i for i, n in enumerate( sorted( set( n for elem in self for n in elem.vertices ) ) ) }
    fmap = { elem: elem.simplex.stdfunc(degree) for elem in self }
    nmap = { elem: numeric.array([ dofmap[n] for n in elem.vertices ]) for elem in self }
    return function.function( fmap=fmap, nmap=nmap, ndofs=len(dofmap), ndims=2 )

  def __add__( self, other ):
    'add topologies'

    if self is other:
      return self
    assert self.ndims == other.ndims
    return UnstructuredTopology( set(self) | set(other), ndims=self.ndims )

  def __sub__( self, other ):
    'add topologies'

    if self is other:
      return self
    assert self.ndims == other.ndims
    return UnstructuredTopology( set(self) - set(other), ndims=self.ndims )

  def __mul__( self, other ):
    'element products'

    elems = util.Product( self, other )
    return UnstructuredTopology( elems, ndims=self.ndims+other.ndims )

  def __getitem__( self, item ):
    'subtopology'

    items = ( self.groups[it] for it in item.split( ',' ) )
    return sum( items, items.next() )

  @log.title
  def elem_eval( self, funcs, ischeme, separate=False ):
    'element-wise evaluation'

    single_arg = not isinstance(funcs,(tuple,list))
    if single_arg:
      funcs = funcs,

    slices = []
    pointshape = function.PointShape().compiled()
    npoints = 0
    separators = []
    __logger__ = log.iter( 'elem', self )
    for elem in __logger__:
      np, = pointshape.eval( elem, ischeme )
      slices.append( slice(npoints,npoints+np) )
      npoints += np
      if separate:
        separators.append( npoints )
        npoints += 1
    if separate:
      separators = numeric.array( separators[:-1], dtype=int )
      npoints -= 1

    retvals = []
    idata = []
    for ifunc, func in enumerate( funcs ):
      func = function.asarray( func )
      retval = parallel.shzeros( (npoints,)+func.shape, dtype=func.dtype )
      if separate:
        retval[separators] = numeric.nan
      if function._isfunc( func ):
        for f, ind in function.blocks( func ):
          idata.append( function.Tuple( [ ifunc, function.Tuple(ind), f ] ) )
      else:
        idata.append( function.Tuple( [ ifunc, (), func ] ) )
      retvals.append( retval )
    idata = function.Tuple( idata ).compiled()

    for ielem, elem in parallel.pariter( enumerate( self ) ):
      s = slices[ielem],
      for ifunc, index, data in idata.eval( elem, ischeme ):
        retvals[ifunc][s+index] = data

    log.info( 'cache', idata.cache.summary() )
    log.info( 'created', ', '.join( '%s(%s)' % ( retval.__class__.__name__, ','.join(map(str,retval.shape)) ) for retval in retvals ) )
    if single_arg:
      retvals, = retvals

    return retvals

  @log.title
  def elem_mean( self, funcs, geometry, ischeme ):
    'element-wise integration'

    single_arg = not isinstance(funcs,(tuple,list))
    if single_arg:
      funcs = funcs,

    retvals = []
    #iweights = geometry.iweights( self.ndims )
    iweights = function.iwscale( geometry, self.ndims )
    idata = [ iweights ]
    for func in funcs:
      func = function.asarray( func )
      if not function._isfunc( func ):
        func = function.Const( func )
      assert all( isinstance(sh,int) for sh in func.shape )
      idata.append( function.elemint( func, iweights ) )
      retvals.append( numeric.empty( (len(self),)+func.shape ) )
    idata = function.Tuple( idata )

    for ielem, elem in enumerate( self ):
      area_data = idata( elem, ischeme )
      area = area_data[0].sum()
      for retval, data in zip( retvals, area_data[1:] ):
        retval[ielem] = data / area

    log.info( 'created', ', '.join( '%s(%s)' % ( retval.__class__.__name__, ','.join(map(str,retval.shape)) ) for retval in retvals ) )
    if single_arg:
      retvals, = retvals

    return retvals

  @log.title
  def grid_eval( self, funcs, geometry, C ):
    'evaluate grid points'

    single_arg = not isinstance(funcs,(tuple,list))
    if single_arg:
      funcs = funcs,

    C = numeric.asarray( C )
    assert C.shape[0] == self.ndims
    shape = C.shape
    C = C.reshape( self.ndims, -1 )

    funcs = [ function.asarray(func) for func in funcs ]
    retvals = [ numeric.empty( C.shape[1:] + func.shape ) for func in funcs ]
    for retval in retvals:
      retval[:] = numeric.nan

    data = function.Tuple([ function.Tuple([ func, retval ]) for func, retval in zip( funcs, retvals ) ])

    __logger__ = log.iter( 'elem', self )
    for elem in __logger__:
      points, selection = geometry.find( elem, C.T )
      if selection is not None:
        for func, retval in data( elem, points ):
          retval[selection] = func

    retvals = [ retval.reshape( shape[1:] + func.shape ) for func, retval in zip( funcs, retvals ) ]
    log.info( 'created', ', '.join( '%s(%s)' % ( retval.__class__.__name__, ','.join(map(str,retval.shape)) ) for retval in retvals ) )
    if single_arg:
      retvals, = retvals

    return retvals

  @log.title
  def integrate_symm( self, funcs, ischeme, geometry=None, iweights=None, force_dense=False ):
    'integrate a symmetric integrand on a product domain' # TODO: find a proper home for this

    single_arg = not isinstance(funcs,list)
    if single_arg:
      funcs = funcs,

    if iweights is None:
      assert geometry is not None, 'conflicting arguments geometry and iweights'
      iweights = function.iwscale( geometry, self.ndims )
    else:
      assert geometry is None, 'conflicting arguments geometry and iweights'
    assert iweights.ndim == 0

    integrands = []
    retvals = []
    for ifunc, func in enumerate( funcs ):
      func = function.asarray( func )
      lock = parallel.Lock()
      if function._isfunc( func ):
        array = parallel.shzeros( func.shape, dtype=float ) if func.ndim != 2 \
           else matrix.DenseMatrix( func.shape ) if force_dense \
           else matrix.SparseMatrix( self.build_graph(func), func.shape[1] )
        for f, ind in function.blocks( func ):
          integrands.append( function.Tuple([ ifunc, lock, function.Tuple(ind), function.elemint( f, iweights ) ]) )
      else:
        array = parallel.shzeros( func.shape, dtype=float )
        if not function._iszero( func ):
          integrands.append( function.Tuple([ ifunc, lock, (), function.elemint( func, iweights ) ]) )
      retvals.append( array )
    idata = function.Tuple( integrands ).compiled()

    __logger__ = log.iter( 'elem', self )
    for elem in parallel.pariter( __logger__ ):
      assert isinstance( elem, element.ProductElement )
      compare_elem = cmp( elem.elem1, elem.elem2 )
      if compare_elem < 0:
        continue
      for ifunc, lock, index, data in idata.eval( elem, ischeme ):
        with lock:
          retvals[ifunc][index] += data
          if compare_elem > 0:
            retvals[ifunc][index[::-1]] += data.T

    log.info( 'cache', idata.cache.summary() )
    log.info( 'created', ', '.join( '%s(%s)' % ( retval.__class__.__name__, ','.join(map(str,retval.shape)) ) for retval in retvals ) )
    if single_arg:
      retvals, = retvals

    return retvals

  @log.title
  def refinedfunc( self, dofaxis, refine, degree ):
    'create refined space by refining dofs in existing one'

    warnings.warn( 'refinedfunc is replaced by refined_by + splinefunc; this function will be removed in future' % ischeme, DeprecationWarning )

    refine = set(refine) # make unique and equip with set operations
  
    # initialize
    topoelems = [] # non-overlapping 'top-level' elements, will make up the new domain
    parentelems = [] # all parents, grandparents etc of topoelems
    nrefine = 0 # number of nested topologies after refinement

    dofmap = dofaxis.dofmap
    topo = self
    while topo: # elements to examine in next level refinement
      nexttopo = []
      refined = set() # refined dofs in current refinement level
      __logger__ = log.iter( 'elem', topo )
      for elem in __logger__: # loop over remaining elements in refinement level 'nrefine'
        dofs = dofmap.get( elem ) # dof numbers for current funcsp object
        if dofs is not None: # elem is a top-level element
          supp = refine.intersection(dofs) # supported dofs that are tagged for refinement
          if supp: # elem supports dofs for refinement
            parentelems.append( elem ) # elem will become a parent
            topoelems.extend( filter(None,elem.children) ) # children will become top-level elements
            refined.update( supp ) # dofs will not be considered in following refinement levels
          else: # elem does not support dofs for refinement
            topoelems.append( elem ) # elem remains a top-level elemnt
        else: # elem is not a top-level element
          parentelems.append( elem ) # elem is a parent
          nexttopo.extend( filter(None,elem.children) ) # examine children in next iteration
      refine -= refined # discard dofs to prevent further consideration
      topo = nexttopo # prepare for next iteration
      nrefine += 1 # update refinement level
    assert not refine, 'unrefined leftover: %s' % refine
    if refined: # last considered level contained refinements
      nrefine += 1 # this raises the total level to nrefine + 1

    # initialize
    dofmap = {} # IEN mapping of new function object
    stdmap = {} # shape function mapping of new function object, plus boolean vector indicating which shapes to retain
    ndofs = 0 # total number of dofs of new function object
  
    topo = self # topology to examine in next level refinement
    __logger__ = log.iter( 'level', range(nrefine) )
    for irefine in __logger__:
  
      funcsp = topo.splinefunc( degree ) # shape functions for level irefine
      (func,(dofaxis,)), = function.blocks( funcsp ) # separate elem-local funcs and global placement index
  
      supported = numeric.ones( funcsp.shape[0], dtype=bool ) # True if dof is contained in topoelems or parentelems
      touchtopo = numeric.zeros( funcsp.shape[0], dtype=bool ) # True if dof touches at least one topoelem
      myelems = [] # all top-level or parent elements in level irefine
      for elem, idofs in log.iter( 'element', dofaxis.dofmap.items() ):
        if elem in topoelems:
          touchtopo[idofs] = True
          myelems.append( elem )
        elif elem in parentelems:
          myelems.append( elem )
        else:
          supported[idofs] = False
  
      keep = numeric.logical_and( supported, touchtopo ) # THE refinement law
      if keep.all() and irefine == nrefine - 1:
        return topo, funcsp
  
      for elem in myelems: # loop over all top-level or parent elements in level irefine
        idofs = dofaxis.dofmap[elem] # local dof numbers
        mykeep = keep[idofs]
        std = func.stdmap[elem]
        assert isinstance(std,element.StdElem)
        if mykeep.all():
          stdmap[elem] = std # use all shapes from this level
        elif mykeep.any():
          stdmap[elem] = std, mykeep # use some shapes from this level
        newdofs = [ ndofs + keep[:idof].sum() for idof in idofs if keep[idof] ] # new dof numbers
        if elem not in self: # at lowest level
          pelem, transform = elem.parent
          newdofs.extend( dofmap[pelem] ) # add dofs of all underlying 'broader' shapes
        dofmap[elem] = numeric.array(newdofs) # add result to IEN mapping of new function object
  
      ndofs += keep.sum() # update total number of dofs
      topo = topo.refined # proceed to next level

    for elem in parentelems:
      del dofmap[elem] # remove auxiliary elements

    funcsp = function.function( stdmap, dofmap, ndofs, self.ndims )
    domain = UnstructuredTopology( topoelems, ndims=self.ndims )

    if hasattr( topo, 'boundary' ):
      allbelems = []
      bgroups = {}
      topo = self # topology to examine in next level refinement
      for irefine in range( nrefine ):
        belemset = set()
        for belem in topo.boundary:
          celem, transform = belem.context
          if celem in topoelems:
            belemset.add( belem )
        allbelems.extend( belemset )
        for btag, belems in topo.boundary.groups.iteritems():
          bgroups.setdefault( btag, [] ).extend( belemset.intersection(belems) )
        topo = topo.refined # proceed to next level
      domain.boundary = UnstructuredTopology( allbelems, ndims=self.ndims-1 )
      domain.boundary.groups = dict( ( tag, UnstructuredTopology( group, ndims=self.ndims-1 ) ) for tag, group in bgroups.items() )

    if hasattr( topo, 'interfaces' ):
      allinterfaces = []
      topo = self # topology to examine in next level refinement
      for irefine in range( nrefine ):
        for ielem in topo.interfaces:
          (celem1,transform1), (celem2,transform2) = ielem.interface
          if celem1 in topoelems:
            while True:
              if celem2 in topoelems:
                allinterfaces.append( ielem )
                break
              if not celem2.parent:
                break
              celem2, transform2 = celem2.parent
          elif celem2 in topoelems:
            while True:
              if celem1 in topoelems:
                allinterfaces.append( ielem )
                break
              if not celem1.parent:
                break
              celem1, transform1 = celem1.parent
        topo = topo.refined # proceed to next level
      domain.interfaces = UnstructuredTopology( allinterfaces, ndims=self.ndims-1 )
  
    return domain, funcsp

  def refine( self, n ):
    'refine entire topology n times'

    return self if n <= 0 else self.refined.refine( n-1 )

  @log.title
  def get_simplices( self, maxrefine ):
    'Getting simplices'

    return [ simplex for elem in self for simplex in elem.get_simplices( maxrefine ) ]

  @log.title
  def get_trimmededges( self, maxrefine ):
    'Getting trimmed edges'

    return [ trimmededge for elem in self for trimmededge in elem.get_trimmededges( maxrefine ) ]

class _StructuredTopology( Topology ):
  'structured topology'

  def __init__( self, structure, periodic=() ):
    'constructor'

    structure = numeric.asarray(structure)
    self.structure = structure
    self.periodic = tuple(periodic)
    self.groups = {}
    Topology.__init__( self, structure.ndim )

  def make_periodic( self, periodic ):
    'add periodicity'

    return StructuredTopology( self.structure, periodic=periodic )

  def __len__( self ):
    'number of elements'

    return sum( elem is not None for elem in self.structure.flat )

  def __iter__( self ):
    'iterate over elements'

    return itertools.ifilter( None, self.structure.flat )

  def __getitem__( self, item ):
    'subtopology'

    if isinstance( item, str ):
      return Topology.__getitem__( self, item )
    if not isinstance( item, tuple ):
      item = item,
    periodic = [ idim for idim in self.periodic if idim < len(item) and item[idim] == slice(None) ]
    return StructuredTopology( self.structure[item], periodic=periodic )

  @cache.property
  def interfaces( self ):
    'interfaces'

    interfaces = []
    eye = numeric.eye(self.ndims-1)
    for idim in range(self.ndims):
      s1 = (slice(None),)*idim + (slice(-1),)
      s2 = (slice(None),)*idim + (slice(1,None),)
      for elem1, elem2 in numeric.broadcast( self.structure[s1], self.structure[s2] ):
        A = numeric.zeros((self.ndims,self.ndims-1))
        A[:idim] = eye[:idim]
        A[idim+1:] = -eye[idim:]
        b = numeric.hstack( [ numeric.zeros(idim+1), numeric.ones(self.ndims-idim) ] )
        context1 = elem1, element.AffineTransformation( b[1:], A )
        context2 = elem2, element.AffineTransformation( b[:-1], A )
        vertices = numeric.asarray( elem1.vertices ).reshape( [2]*elem1.ndims )[s2].ravel()
        assert numeric.equal( vertices == numeric.asarray( elem2.vertices ).reshape( [2]*elem1.ndims )[s1].ravel() ).all()
        ielem = element.QuadElement( ndims=self.ndims-1, vertices=vertices, interface=(context1,context2) )
        interfaces.append( ielem )
    return UnstructuredTopology( interfaces, ndims=self.ndims-1 )

  def discontfunc( self, degree ):
    'discontinuous shape functions'

    if isinstance( degree, int ):
      degree = ( degree, ) * self.ndims

    dofs = numeric.arange( numeric.product(numeric.array(degree)+1) * len(self) ).reshape( len(self), -1 )
    dofmap = dict( zip( self, dofs ) )

    stdelem = util.product( element.PolyLine( element.PolyLine.bernstein_poly( d ) ) for d in degree )
    funcmap = dict( numeric.broadcast( self.structure, stdelem ) )

    return function.function( funcmap, dofmap, dofs.size, self.ndims )

  def curvefreesplinefunc( self ):
    'spline from vertices'

    p = 2
    periodic = self.periodic

    vertex_structure = numeric.array( 0 )
    dofcount = 1
    slices = []

    for idim in range( self.ndims ):
      periodic_i = idim in periodic
      n = self.structure.shape[idim]

      stdelems_i = element.PolyLine.spline( degree=p, nelems=n, curvature=True )

      stdelems = stdelems[...,_] * stdelems_i if idim else stdelems_i

      nd = n + p - 2
      numbers = numeric.arange( nd )

      vertex_structure = vertex_structure[...,_] * nd + numbers

      dofcount *= nd

      myslice = [ slice(0,2) ]
      for i in range(n-2):
        myslice.append( slice(i,i+p+1) )
      myslice.append( slice(n-2,n) )

      slices.append( myslice )

    dofmap = {}
    for item in numeric.broadcast( self.structure, *numeric.ix_(*slices) ):
      elem = item[0]
      S = item[1:]
      dofmap[ elem ] = vertex_structure[S].ravel()

    dofaxis = function.DofMap( ElemMap(dofmap,self.ndims) )
    funcmap = dict( numeric.broadcast( self.structure, stdelems ) )

    return function.Function( dofaxis=dofaxis, stdmap=ElemMap(funcmap,self.ndims), igrad=0 )

  def __str__( self ):
    'string representation'

    return '%s(%s)' % ( self.__class__.__name__, 'x'.join(map(str,self.structure.shape)) )

  @cache.property
  def multiindex( self ):
    'Inverse map of self.structure: given an element find its location in the structure.'
    return dict( (self.structure[alpha], alpha) for alpha in numeric.ndindex( self.structure.shape ) )

  def neighbor( self, elem0, elem1 ):
    'Neighbor detection, returns codimension of interface, -1 for non-neighboring elements.'

    return elem0.neighbor( elem1 )

    # REPLACES:
    alpha0 = self.multiindex[elem0]
    alpha1 = self.multiindex[elem1]
    diff = numeric.array(alpha0) - numeric.array(alpha1)
    for i, shi in enumerate( self.structure.shape ):
      if diff[i] in (shi-1, 1-shi) and i in self.periodic:
        diff[i] = -numeric.sign( shi )
    if set(diff).issubset( (-1,0,1) ):
      return numeric.sum(numeric.abs(diff))
    return -1
    
class _UnstructuredTopology( Topology ):
  'externally defined topology'

  def __init__( self, elements, ndims, namedfuncs={} ):
    'constructor'

    self.namedfuncs = namedfuncs
    self.elements = elements
    self.groups = {}
    Topology.__init__( self, ndims )

  def __iter__( self ):
    'iterate over elements'

    return iter( self.elements )

  def __len__( self ):
    'number of elements'

    return len(self.elements)

  def splinefunc( self, degree ):
    'spline func'

    return self.namedfuncs[ 'spline%d' % degree ]

  def bubblefunc( self ):
    'linear func + bubble'

    return self.namedfuncs[ 'bubble1' ]

class _HierarchicalTopology( Topology ):
  'collection of nested topology elments'

  def __init__( self, basetopo, elements ):
    'constructor'

    if isinstance( basetopo, HierarchicalTopology ):
      basetopo = basetopo.basetopo
    self.basetopo = basetopo
    self.elements = tuple(elements)
    Topology.__init__( self, basetopo.ndims )

  def __iter__( self ):
    'iterate over elements'

    return iter(self.elements)

  def __len__( self ):
    'number of elements'

    return len(self.elements)

  @cache.property
  def boundary( self ):
    'boundary elements & groups'

    assert hasattr( self.basetopo, 'boundary' )
    allbelems = []
    bgroups = {}
    topo = self.basetopo # topology to examine in next level refinement
    for irefine in range( nrefine ):
      belemset = set()
      for belem in topo.boundary:
        celem, transform = belem.context
        if celem in self.elems:
          belemset.add( belem )
      allbelems.extend( belemset )
      for btag, belems in topo.boundary.groups.iteritems():
        bgroups.setdefault( btag, [] ).extend( belemset.intersection(belems) )
      topo = topo.refined # proceed to next level
    boundary = UnstructuredTopology( allbelems, ndims=self.ndims-1 )
    boundary.groups = dict( ( tag, UnstructuredTopology( group, ndims=self.ndims-1 ) ) for tag, group in bgroups.items() )
    return boundary

  @cache.property
  def interfaces( self ):
    'interface elements & groups'

    assert hasattr( self.basetopo, 'interfaces' )
    allinterfaces = []
    topo = self.basetopo # topology to examine in next level refinement
    for irefine in range( nrefine ):
      for ielem in topo.interfaces:
        (celem1,transform1), (celem2,transform2) = ielem.interface
        if celem1 in self.elems:
          while True:
            if celem2 in self.elems:
              allinterfaces.append( ielem )
              break
            if not celem2.parent:
              break
            celem2, transform2 = celem2.parent
        elif celem2 in self.elems:
          while True:
            if celem1 in self.elems:
              allinterfaces.append( ielem )
              break
            if not celem1.parent:
              break
            celem1, transform1 = celem1.parent
      topo = topo.refined # proceed to next level
    return UnstructuredTopology( allinterfaces, ndims=self.ndims-1 )

  @log.title
  def _funcspace( self, mkspace ):

    dofmap = {} # IEN mapping of new function object
    stdmap = {} # shape function mapping of new function object, plus boolean vector indicating which shapes to retain
    ndofs = 0 # total number of dofs of new function object
    remaining = len(self) # element count down (know when to stop)
  
    topo = self.basetopo # topology to examine in next level refinement
    newdiscard = []
    parentelems = []
    maxrefine = 9
    for irefine in range( maxrefine ):

      funcsp = mkspace( topo ) # shape functions for level irefine
      (func,(dofaxis,)), = function.blocks( funcsp ) # separate elem-local funcs and global placement index
  
      discard = set(newdiscard)
      newdiscard = []
      supported = numeric.ones( funcsp.shape[0], dtype=bool ) # True if dof is contained in topoelems or parentelems
      touchtopo = numeric.zeros( funcsp.shape[0], dtype=bool ) # True if dof touches at least one topoelem
      myelems = [] # all top-level or parent elements in level irefine
      for elem, idofs in dofaxis.dofmap.items():
        if elem in self.elements:
          remaining -= 1
          touchtopo[idofs] = True
          myelems.append( elem )
          newdiscard.append( elem )
        else:
          pelem, trans = elem.parent
          if pelem in discard:
            newdiscard.append( elem )
            supported[idofs] = False
          else:
            parentelems.append( elem )
            myelems.append( elem )
  
      keep = numeric.logical_and( supported, touchtopo ) # THE refinement law

      for elem in myelems: # loop over all top-level or parent elements in level irefine
        idofs = dofaxis.dofmap[elem] # local dof numbers
        mykeep = keep[idofs]
        std = func.stdmap[elem]
        assert isinstance(std,element.StdElem)
        if mykeep.all():
          stdmap[elem] = std # use all shapes from this level
        elif mykeep.any():
          stdmap[elem] = std, mykeep # use some shapes from this level
        newdofs = [ ndofs + keep[:idof].sum() for idof in idofs if keep[idof] ] # new dof numbers
        if irefine: # not at lowest level
          pelem, transform = elem.parent
          newdofs.extend( dofmap[pelem] ) # add dofs of all underlying 'broader' shapes
        dofmap[elem] = numeric.array(newdofs) # add result to IEN mapping of new function object
  
      ndofs += int( keep.sum() ) # update total number of dofs
      if not remaining:
        break
      topo = topo.refined # proceed to next level
  
    else:

      raise Exception, 'elements remaining after %d iterations' % maxrefine

    for elem in parentelems:
      del dofmap[elem] # remove auxiliary elements

    return function.function( stdmap, dofmap, ndofs, self.ndims )

  def stdfunc( self, *args, **kwargs ):
    return self._funcspace( lambda topo: topo.stdfunc( *args, **kwargs ) )

  def splinefunc( self, *args, **kwargs ):
    return self._funcspace( lambda topo: topo.splinefunc( *args, **kwargs ) )

@log.title
def glue( master, slave, geometry, tol=1.e-10, verbose=False ):
  'Glue topologies along boundary group __glue__.'

  gluekey = '__glue__'

  # Checks on input
  assert gluekey in master.boundary.groups and \
         gluekey in slave.boundary.groups, 'Must identify glue boundary first.'
  assert len(master.boundary[gluekey]) == \
          len(slave.boundary[gluekey]), 'Minimum requirement is that cardinality is equal.'
  assert master.ndims == 2 and slave.ndims == 2, '1D boundaries for now.' # see dists computation and update_vertices

  _mg, _sg = geometry if isinstance( geometry, tuple ) else (geometry,) * 2
  master_geom = _mg.compiled()
  slave_geom = master_geom if _mg == _sg else _sg.compiled()

  nglue = len(master.boundary[gluekey])
  assert len(slave.boundary[gluekey]) == nglue

  log.info( 'pairing elements [%i]' % nglue )
  masterelems = [ ( masterelem, master_geom.eval( masterelem, 'gauss1' )[0] ) for masterelem in master.boundary[gluekey] ]
  elempairs = []
  maxdist = 0
  for slaveelem in slave.boundary[gluekey]:
    slavepoint = slave_geom.eval( slaveelem, 'gauss1' )[0]
    distances = [ numeric.norm2( masterpoint - slavepoint ) for masterelem, masterpoint in masterelems ]
    i = numeric.numpy.argmin( distances )
    maxdist = max( maxdist, distances[i] )
    elempairs.append(( masterelems.pop(i)[0], slaveelem ))
  assert not masterelems
  assert maxdist < tol, 'maxdist exceeds tolerance: %.2e >= %.2e' % ( maxdist, tol )
  log.info( 'all elements matched within %.2e radius' % maxdist )

  # convert element pairs to vertex map
  vtxmap = {}
  for masterelem, slave_elem in elempairs:
    for oldvtx, newvtx in zip( slave_elem.vertices, reversed(masterelem.vertices) ):
      assert vtxmap.setdefault( oldvtx, newvtx ) == newvtx, 'conflicting vertex info'

  emap = {} # elem->newelem map
  for belem in slave.boundary:
    if not any( vtx in vtxmap for vtx in belem.vertices ):
      continue
    emap[belem] = element.QuadElement( belem.ndims,
      vertices=[ vtxmap.get(vtx,vtx) for vtx in belem.vertices ],
      parent=(belem,transform.Identity(belem.ndims)) )
    elem, trans = belem.context
    emap[elem] = element.QuadElement( elem.ndims,
      vertices=[ vtxmap.get(vtx,vtx) for vtx in elem.vertices ],
      parent=(elem,transform.Identity(elem.ndims)) )

  _wrapelem = lambda elem: emap.get(elem,elem)
  def _wraptopo( topo ):
    elems = map( _wrapelem, topo )
    return UnstructuredTopology( elems, ndims=topo.ndims ) if not isinstance( topo, UnstructuredTopology ) \
      else StructuredTopology( numeric.asarray(elems).reshape(slave.structure.shape) )

  # generate glued topology
  elems = list( master ) + map( _wrapelem, slave )
  union = UnstructuredTopology( elems, master.ndims )
  union.groups['master'] = master
  union.groups['slave'] = _wraptopo(slave)
  union.groups.update({ 'master_'+key: topo for key, topo in master.groups.iteritems() })
  union.groups.update({ 'slave_' +key: _wraptopo(topo) for key, topo in slave.groups.iteritems() })

  # generate topology boundary
  belems = [ belem for belem in master.boundary if belem not in master.boundary[gluekey] ] \
    + [ _wrapelem(belem) for belem in slave.boundary if belem not in slave.boundary[gluekey] ]
  union.boundary = UnstructuredTopology( belems, master.ndims-1 )
  union.boundary.groups['master'] = master.boundary
  union.boundary.groups['slave'] = _wraptopo(slave.boundary)
  union.boundary.groups.update({ 'master_'+key: topo for key, topo in master.boundary.groups.iteritems() if key != gluekey })
  union.boundary.groups.update({ 'slave_' +key: _wraptopo(topo) for key, topo in slave.boundary.groups.iteritems() if key != gluekey })

  log.info( 'created glued topology [%i]' % len(union) )
  return union

# vim:shiftwidth=2:foldmethod=indent:foldnestmax=2
