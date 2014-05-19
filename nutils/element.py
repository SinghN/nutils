from __future__ import division
from . import log, util, cache, numeric, transform, function, _
import warnings, weakref


NODEFAULT = object()

class Element( cache.Immutable ):

  def __init__( self, ndims ):
    self.ndims = ndims

  def _check_divergence( self, decimal=10 ):
    from pointset import Pointset
    gauss = Pointset( 'gauss', 1 )
    x, w = gauss( self )
    volume = w.sum()
    check_volume = 0
    check_zero = 0
    for trans, edge in self.edges:
      xe, we = gauss( edge )
      w_normal = numeric.times( we, trans.det )
      check_zero += w_normal.sum(0)
      check_volume += numeric.contract( trans.apply(xe), w_normal, axis=0 )
    numeric.testing.assert_almost_equal( check_zero, 0, decimal, '%s fails divergence test' % self )
    numeric.testing.assert_almost_equal( check_volume, volume, decimal, '%s fails divergence test' % self )

  @property
  def edge2vertex( self ):
    return [ numeric.array([ numeric.find( numeric.equal( self.vertices, trans.apply(v) ).all( axis=1 ) )[0] for v in edge.vertices ])
      for trans, edge in self.edges ]

  def get_edge( self, trans, default=NODEFAULT ):
    i = numeric.bisect( self.edges, (trans,) )
    if i >= 0:
      trans_, edge = self.edges[i]
      if trans_ == trans:
        return edge
    assert default is not NODEFAULT, 'no edge found for transformation %s' % trans
    return default

  @property
  def edgedict( self ):
    return dict( self.edges )

  @property
  def childdict( self ):
    return dict( self.children )

class Mosaic( Element ):

  def __init__( self, ndims, children, edges=None ):
    self.children = children
    self.edges = edges
    Element.__init__( self, ndims )

    if edges:
      self._check_divergence()

  def pointset( self, pointset ):
    allpoints = []
    allweights = []
    for trans, child in self.simplices:
      points, weights = pointset( child )
      allpoints.append( trans.apply( points ) )
      if weights:
        allweights.append( numeric.times( weights, trans.det ) if trans.sign
                      else weights * abs(trans.det) )
    return numeric.concatenate( allpoints, axis=0 ), \
      numeric.concatenate( allweights, axis=0 ) if len(allweights) == len(allpoints) else None

  @property
  def flipped( self ): # flip transformation as deep as possible to keep cascade intact
    children = tuple( (trans,elem.flipped) if isinstance(elem,Mosaic)
                 else (trans.flipped,elem) for trans, elem in self.children )
    return Mosaic( self.ndims, children )

  @cache.property
  def simplices( self ): # merge transformations recursively
    return [ (ctrans*strans,shead) for ctrans, chead in self.children for strans, shead in chead.simplices ]

class Reference( Element ):

  def __init__( self, vertices ):
    self.vertices = numeric.asarray( vertices )
    assert self.vertices.dtype == int
    self.nverts, ndims = self.vertices.shape
    Element.__init__( self, ndims )

  @property
  def simplices( self ):
    return [ (transform.Identity(self.ndims),self) ]

  def __mul__( self, other ):
    assert isinstance( other, Reference )
    return other if self.ndims == 0 \
      else self if other.ndims == 0 \
      else Tensor( self, other )

  def __pow__( self, n ):
    assert isinstance( n, int ) and n >= 1
    return self if n == 1 else self * self**(n-1)

  def trim( self, levelset, maxrefine, minrefine, numer ):

    assert maxrefine >= minrefine >= 0
    if minrefine == 0 and not numeric.isarray( levelset ):
      from pointset import Pointset
      vertex = Pointset( 'vertex', maxrefine )
      levelset = levelset[-1].eval( (levelset[:-1],self), vertex )

    elems = [], []
    belems = [], []
    identity = transform.Identity(self.ndims)

    if maxrefine == 0: # check values

      repeat = True
      while repeat: # set almost-zero points to zero if cutoff within eps
        repeat = False
        assert levelset.shape == (self.nverts,)
        if numeric.greater_equal( levelset, 0 ).all():
          return None, self
        if numeric.less_equal( levelset, 0 ).all():
          return self, None
        isects = []
        for ribbon in self.ribbon2vertices:
          a, b = levelset[ribbon]
          if a * b < 0: # strict sign change
            x = int( numer * a / float(a-b) + .5 ) # round to [0,1,..,numer]
            if 0 < x < numer:
              isects.append(( x, ribbon ))
            else: # near intersection of vertex
              v = ribbon[ (0,numer).index(x) ]
              log.debug( 'rounding vertex #%d from %f to 0' % ( v, levelset[v] ) )
              levelset[v] = 0
              repeat = True

      coords_int = numeric.vstack([
        self.vertices * numer,
        [ numeric.dot( (numer-x,x), self.vertices[ribbon] ) for x, ribbon in isects ]
      ])

      simplex = Simplex( self.ndims )
      triinfo = []

      for tri in util.delaunay( coords_int ):

        ispos = isneg = False
        for ivert in tri:
          if ivert < self.nverts:
            sign = levelset[ivert]
            ispos = ispos or sign > 0
            isneg = isneg or sign < 0
        assert ispos is not isneg, 'domains do not separate in two convex parts'

        offset = coords_int[tri[0]]
        matrix = ( coords_int[tri[1:]] - offset ).T
        if numeric.det( matrix.astype(float) ) < 0:
          tri[-2:] = tri[-1], tri[-2]
          matrix = ( coords_int[tri[1:]] - offset ).T
        trans = transform.affinetrans( matrix, offset, numer, 0 )
        elems[ispos].append( (trans,simplex) )

        triinfo.append(( tri, trans, ispos )) # for edges

      for iedge, vertices in enumerate( self.edge2vertex ):
        etrans, edge = self.edges[iedge]
        mask = numeric.zeros( len(self.vertices)+len(isects), dtype=bool )
        mask[vertices] = True
        mask[len(self.vertices):] = [ mask[ribbon].all() for x, ribbon in isects ]

        collect = [], []
        for tri, trans, ispos in triinfo:
          not_on_edge = numeric.find( ~mask[tri] )
          if len( not_on_edge ) == 1:
            iedge, = not_on_edge
            collect[ispos].append(( iedge, trans ))

        if not collect[1]:
          belems[0].append(( etrans, edge ))
        elif not collect[0]:
          belems[1].append(( etrans, edge ))
        else:
          for ispos in range(2):
            btri = []
            for iedge, trans in collect[ispos]:
              etrans2, edge2 = simplex.edges[iedge]
              result = transform.solve( trans * etrans2, etrans )
              btri.append(( result, edge2 ))
            belems[ispos].append(( etrans, Mosaic( self.ndims-1, tuple(btri) ) ))

      collect = [], []
      for tri, trans, ispos in triinfo:
        extra = [ n for n, i in enumerate(tri) if i < self.nverts and levelset[i] != 0 ]
        if len(extra) == 1:
          iedge, = extra # all vertices except for iedge lie on the interface
          etrans, esimplex = simplex.edges[iedge]
          collect[ispos].append((trans*etrans,esimplex))
      for ispos in range(2):
        mosaic = Mosaic( self.ndims-1, tuple(collect[ispos]) )
        belems[ispos].append(( identity, mosaic ))

    else: # recurse

      if minrefine == 0:
        nverts, subs = self._child_subsets[maxrefine-1]
        assert levelset.shape == (nverts,)
        sub = iter(subs)
      for trans, elem in self.children:
        n, p = elem.trim( levelset[:-1]+(trans,)+levelset[-1:], maxrefine-1, minrefine-1, numer ) if minrefine \
          else elem.trim( levelset[sub.next()], maxrefine-1, 0, numer )
        if n: elems[0].append((trans,n))
        if p: elems[1].append((trans,p))
      if not elems[0]:
        return None, self
      if not elems[1]:
        return self, None

      for ispos in range(2):
        iface = tuple( (trans,elem.edgedict[identity]) for trans, elem in elems[ispos] if isinstance(elem,Mosaic) )
        assert iface # there must be at least one mosaic element in both pos and neg
        belems[ispos].append(( identity, Mosaic( self.ndims-1, iface ) ))

      for updim, edge in self.edges:

        bpos = []
        bneg = []

        for scale, childedge_untrimmed in edge.children:
          scale2, updim2 = transform.prioritize( (updim,scale), ndims=self.ndims )
          assert updim == updim2

          eneg, = [ e.edgedict.get(updim2) for t, e in elems[0] if t == scale2 ] or [ None ]
          if eneg: bneg.append(( scale, eneg ))

          epos, = [ e.edgedict.get(updim2) for t, e in elems[1] if t == scale2 ] or [ None ]
          if epos: bpos.append(( scale, epos ))

          assert epos or eneg

        if not bpos:
          belems[0].append(( updim, edge ))
        elif not bneg:
          belems[1].append(( updim, edge ))
        else:
          belems[0].append(( updim, Mosaic(self.ndims-1,tuple(bneg)) ))
          belems[1].append(( updim, Mosaic(self.ndims-1,tuple(bpos)) ))

    return [ Mosaic( self.ndims, tuple(elems[i]), tuple(belems[i]) ) for i in range(2) ]

class Simplex( Reference ):

  def __init__( self, ndims ):
    'constructor'

    assert ndims >= 0
    vertices = numeric.concatenate( [ numeric.zeros(ndims,dtype=int)[_,:],
                                      numeric.eye(ndims,dtype=int) ], axis=0 )
    Reference.__init__( self, vertices )

    if ndims == 0: # point
      return

    edge = Simplex( ndims-1 )

    if ndims == 1: # line
      trans0 = transform.Point( +1 ) + [1]
      trans1 = transform.Point( -1 )
      self.edges = (trans0,edge), (trans1,edge)
      scale = transform.ScaleUniform( 1, 2 )
      self.children = (scale,self), (scale+[.5],self)
      return

    eye2 = numeric.vstack( [numeric.eye( ndims, dtype=int )]*2 ) # ndims*2 x ndims
    orig = numeric.zeros(ndims,dtype=int)

    if ndims == 2:
      self.edges = [ ( transform.affinetrans( (vertices[2:]-vertices[1]).T, vertices[1], 1, -1 ), edge ) ] \
        + [ ( transform.affinetrans( (eye2[idim:][1:ndims]).T, orig, 1, [+1,-1][idim] ), edge ) for idim in range(ndims) ]
    elif ndims == 3:
      self.edges = [ ( transform.affinetrans( (vertices[2:]-vertices[1]).T, vertices[1], 1, +1 ), edge ) ] \
        + [ ( transform.affinetrans( (eye2[idim:][1:ndims]).T, orig, 1, [-1,-1,-1][idim] ), edge ) for idim in range(ndims) ]
    # TODO implement edges for arbitrary dimension

    assert all( trans.fromdim == ndims-1 and trans.todim == ndims for trans, edge_ in self.edges )
    assert len( self.edges ) == self.ndims + 1

    if ndims == 2: # triangle
      scale = transform.ScaleUniform( 2, 2 )
      negscale = transform.ScaleUniform( 2, -2 )
      self.children = (
        ( scale, self ),
        ( scale + [.5, 0], self ),
        ( scale + [ 0,.5], self ),
        ( negscale + [.5,.5], self ),
      )

    self._check_divergence()

  def __str__( self ):
    return 'Simplex(%d)' % self.ndims

  __repr__ = __str__

  @cache.property
  def OLDedge2vertex( self ):
    return ~numeric.eye( self.ndims+1, dtype=bool )

  @cache.property
  def ribbon2vertices( self ):
    return numeric.array([ (i,j) for i in range( self.ndims+1 ) for j in range( i+1, self.ndims+1 ) ])

  @cache.propertylist
  def _child_subsets( self, level ):
    if self.ndims != 1:
      raise NotImplementedError
    n = 2**(level+1) + 1
    indices = numeric.arange(n)
    return n, ( indices[:n//2+1], indices[n//2:] )

  def stdfunc( self, degree ):
    if self.ndims == 1:
      return PolyLine( PolyLine.bernstein_poly( degree ) )
    elif self.ndims == 2:
      return PolyTriangle( degree )
    raise NotImplementedError

  def pointset_vtk( self ):
    assert self.ndims in (2,3)
    return self.vertices, numeric.ones(self.nverts)

  def pointset_gauss( self, degree ):
    assert isinstance( degree, int ) and degree >= 0
    if self.ndims == 0: # point
      return numeric.zeros((1,0)), numeric.ones(1)
    if self.ndims == 1: # line
      k = numeric.arange( 1, degree // 2 + 1 )
      d = k / numeric.sqrt( 4*k**2-1 )
      x, w = numeric.eigh( numeric.diagflat(d,-1) ) # eigh operates (by default) on lower triangle
      return (x[:,_]+1) * .5, w[0]**2
    if self.ndims == 2: # triangle: http://www.cs.rpi.edu/~flaherje/pdf/fea6.pdf
      if degree == 1:
        coords = numeric.array( [[1],[1]] ) / 3.
        weights = numeric.array( [1] ) / 2.
      elif degree == 2:
        coords = numeric.array( [[4,1,1],[1,4,1]] ) / 6.
        weights = numeric.array( [1,1,1] ) / 6.
      elif degree == 3:
        coords = numeric.array( [[5,9,3,3],[5,3,9,3]] ) / 15.
        weights = numeric.array( [-27,25,25,25] ) / 96.
      elif degree == 4:
        A = 0.091576213509771; B = 0.445948490915965; W = 0.109951743655322
        coords = numeric.array( [[1-2*A,A,A,1-2*B,B,B],[A,1-2*A,A,B,1-2*B,B]] )
        weights = numeric.array( [W,W,W,1/3.-W,1/3.-W,1/3.-W] ) / 2.
      elif degree == 5:
        A = 0.101286507323456; B = 0.470142064105115; V = 0.125939180544827; W = 0.132394152788506
        coords = numeric.array( [[1./3,1-2*A,A,A,1-2*B,B,B],[1./3,A,1-2*A,A,B,1-2*B,B]] )
        weights = numeric.array( [1-3*V-3*W,V,V,V,W,W,W] ) / 2.
      elif degree == 6:
        A = 0.063089014491502; B = 0.249286745170910; C = 0.310352451033785; D = 0.053145049844816; V = 0.050844906370207; W = 0.116786275726379
        VW = 1/6. - (V+W) / 2.
        coords = numeric.array( [[1-2*A,A,A,1-2*B,B,B,1-C-D,1-C-D,C,C,D,D],[A,1-2*A,A,B,1-2*B,B,C,D,1-C-D,D,1-C-D,C]] )
        weights = numeric.array( [V,V,V,W,W,W,VW,VW,VW,VW,VW,VW] ) / 2.
      elif degree == 7:
        A = 0.260345966079038; B = 0.065130102902216; C = 0.312865496004875; D = 0.048690315425316; U = 0.175615257433204; V = 0.053347235608839; W = 0.077113760890257
        coords = numeric.array( [[1./3,1-2*A,A,A,1-2*B,B,B,1-C-D,1-C-D,C,C,D,D],[1./3,A,1-2*A,A,B,1-2*B,B,C,D,1-C-D,D,1-C-D,C]] )
        weights = numeric.array( [1-3*U-3*V-6*W,U,U,U,V,V,V,W,W,W,W,W,W] ) / 2.
      else:
        raise NotImplementedError
    elif self.ndims == 3: # tetrahedron: http://people.sc.fsu.edu/~jburkardt/datasets/quadrature_rules_tet/quadrature_rules_tet.html'''
      if degree == 1:
        coords = numeric.array( [[1],[1],[1]] ) / 4.
        weights = numeric.array( [1] ) / 6.
      elif degree == 2:
        coords = numeric.array([[0.5854101966249685,0.1381966011250105,0.1381966011250105],
                                [0.1381966011250105,0.1381966011250105,0.1381966011250105],
                                [0.1381966011250105,0.1381966011250105,0.5854101966249685],
                                [0.1381966011250105,0.5854101966249685,0.1381966011250105]]).T
        weights = numeric.array([1,1,1,1]) / 24.
      elif degree == 3:
        coords = numeric.array([[0.2500000000000000,0.2500000000000000,0.2500000000000000],
                                [0.5000000000000000,0.1666666666666667,0.1666666666666667],
                                [0.1666666666666667,0.1666666666666667,0.1666666666666667],
                                [0.1666666666666667,0.1666666666666667,0.5000000000000000],
                                [0.1666666666666667,0.5000000000000000,0.1666666666666667]]).T
        weights = numeric.array([-0.8000000000000000,0.4500000000000000,0.4500000000000000,0.4500000000000000,0.4500000000000000]) / 6.
      elif degree == 4:
        coords = numeric.array([[0.2500000000000000,0.2500000000000000,0.2500000000000000],
                                [0.7857142857142857,0.0714285714285714,0.0714285714285714],
                                [0.0714285714285714,0.0714285714285714,0.0714285714285714],
                                [0.0714285714285714,0.0714285714285714,0.7857142857142857],
                                [0.0714285714285714,0.7857142857142857,0.0714285714285714],
                                [0.1005964238332008,0.3994035761667992,0.3994035761667992],
                                [0.3994035761667992,0.1005964238332008,0.3994035761667992],
                                [0.3994035761667992,0.3994035761667992,0.1005964238332008],
                                [0.3994035761667992,0.1005964238332008,0.1005964238332008],
                                [0.1005964238332008,0.3994035761667992,0.1005964238332008],
                                [0.1005964238332008,0.1005964238332008,0.3994035761667992]]).T
        weights = numeric.array([-0.0789333333333333,0.0457333333333333,0.0457333333333333,0.0457333333333333,0.0457333333333333,0.1493333333333333,0.1493333333333333,0.1493333333333333,0.1493333333333333,0.1493333333333333,0.1493333333333333]) / 6.
      elif degree == 5:
        coords = numeric.array([[0.2500000000000000,0.2500000000000000,0.2500000000000000],
                              [0.0000000000000000,0.3333333333333333,0.3333333333333333],
                              [0.3333333333333333,0.3333333333333333,0.3333333333333333],
                              [0.3333333333333333,0.3333333333333333,0.0000000000000000],
                              [0.3333333333333333,0.0000000000000000,0.3333333333333333],
                              [0.7272727272727273,0.0909090909090909,0.0909090909090909],
                              [0.0909090909090909,0.0909090909090909,0.0909090909090909],
                              [0.0909090909090909,0.0909090909090909,0.7272727272727273],
                              [0.0909090909090909,0.7272727272727273,0.0909090909090909],
                              [0.4334498464263357,0.0665501535736643,0.0665501535736643],
                              [0.0665501535736643,0.4334498464263357,0.0665501535736643],
                              [0.0665501535736643,0.0665501535736643,0.4334498464263357],
                              [0.0665501535736643,0.4334498464263357,0.4334498464263357],
                              [0.4334498464263357,0.0665501535736643,0.4334498464263357],
                              [0.4334498464263357,0.4334498464263357,0.0665501535736643]]).T
        weights = numeric.array([0.1817020685825351,0.0361607142857143,0.0361607142857143,0.0361607142857143,0.0361607142857143,0.0698714945161738,0.0698714945161738,0.0698714945161738,0.0698714945161738,0.0656948493683187,0.0656948493683187,0.0656948493683187,0.0656948493683187,0.0656948493683187,0.0656948493683187]) / 6.
      elif degree == 6:
        coords = numeric.array([[0.3561913862225449,0.2146028712591517,0.2146028712591517],
                              [0.2146028712591517,0.2146028712591517,0.2146028712591517],
                              [0.2146028712591517,0.2146028712591517,0.3561913862225449],
                              [0.2146028712591517,0.3561913862225449,0.2146028712591517],
                              [0.8779781243961660,0.0406739585346113,0.0406739585346113],
                              [0.0406739585346113,0.0406739585346113,0.0406739585346113],
                              [0.0406739585346113,0.0406739585346113,0.8779781243961660],
                              [0.0406739585346113,0.8779781243961660,0.0406739585346113],
                              [0.0329863295731731,0.3223378901422757,0.3223378901422757],
                              [0.3223378901422757,0.3223378901422757,0.3223378901422757],
                              [0.3223378901422757,0.3223378901422757,0.0329863295731731],
                              [0.3223378901422757,0.0329863295731731,0.3223378901422757],
                              [0.2696723314583159,0.0636610018750175,0.0636610018750175],
                              [0.0636610018750175,0.2696723314583159,0.0636610018750175],
                              [0.0636610018750175,0.0636610018750175,0.2696723314583159],
                              [0.6030056647916491,0.0636610018750175,0.0636610018750175],
                              [0.0636610018750175,0.6030056647916491,0.0636610018750175],
                              [0.0636610018750175,0.0636610018750175,0.6030056647916491],
                              [0.0636610018750175,0.2696723314583159,0.6030056647916491],
                              [0.2696723314583159,0.6030056647916491,0.0636610018750175],
                              [0.6030056647916491,0.0636610018750175,0.2696723314583159],
                              [0.0636610018750175,0.6030056647916491,0.2696723314583159],
                              [0.2696723314583159,0.0636610018750175,0.6030056647916491],
                              [0.6030056647916491,0.2696723314583159,0.0636610018750175]]).T
        weights = numeric.array([0.0399227502581679,0.0399227502581679,0.0399227502581679,0.0399227502581679,0.0100772110553207,0.0100772110553207,0.0100772110553207,0.0100772110553207,0.0553571815436544,0.0553571815436544,0.0553571815436544,0.0553571815436544,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857,0.0482142857142857]) / 6.
      elif degree == 7:
        coords = numeric.array([[0.2500000000000000,0.2500000000000000,0.2500000000000000],
                              [0.7653604230090441,0.0782131923303186,0.0782131923303186],
                              [0.0782131923303186,0.0782131923303186,0.0782131923303186],
                              [0.0782131923303186,0.0782131923303186,0.7653604230090441],
                              [0.0782131923303186,0.7653604230090441,0.0782131923303186],
                              [0.6344703500082868,0.1218432166639044,0.1218432166639044],
                              [0.1218432166639044,0.1218432166639044,0.1218432166639044],
                              [0.1218432166639044,0.1218432166639044,0.6344703500082868],
                              [0.1218432166639044,0.6344703500082868,0.1218432166639044],
                              [0.0023825066607383,0.3325391644464206,0.3325391644464206],
                              [0.3325391644464206,0.3325391644464206,0.3325391644464206],
                              [0.3325391644464206,0.3325391644464206,0.0023825066607383],
                              [0.3325391644464206,0.0023825066607383,0.3325391644464206],
                              [0.0000000000000000,0.5000000000000000,0.5000000000000000],
                              [0.5000000000000000,0.0000000000000000,0.5000000000000000],
                              [0.5000000000000000,0.5000000000000000,0.0000000000000000],
                              [0.5000000000000000,0.0000000000000000,0.0000000000000000],
                              [0.0000000000000000,0.5000000000000000,0.0000000000000000],
                              [0.0000000000000000,0.0000000000000000,0.5000000000000000],
                              [0.2000000000000000,0.1000000000000000,0.1000000000000000],
                              [0.1000000000000000,0.2000000000000000,0.1000000000000000],
                              [0.1000000000000000,0.1000000000000000,0.2000000000000000],
                              [0.6000000000000000,0.1000000000000000,0.1000000000000000],
                              [0.1000000000000000,0.6000000000000000,0.1000000000000000],
                              [0.1000000000000000,0.1000000000000000,0.6000000000000000],
                              [0.1000000000000000,0.2000000000000000,0.6000000000000000],
                              [0.2000000000000000,0.6000000000000000,0.1000000000000000],
                              [0.6000000000000000,0.1000000000000000,0.2000000000000000],
                              [0.1000000000000000,0.6000000000000000,0.2000000000000000],
                              [0.2000000000000000,0.1000000000000000,0.6000000000000000],
                              [0.6000000000000000,0.2000000000000000,0.1000000000000000]]).T
        weights = numeric.array([0.1095853407966528,0.0635996491464850,0.0635996491464850,0.0635996491464850,0.0635996491464850,-0.3751064406859797,-0.3751064406859797,-0.3751064406859797,-0.3751064406859797,0.0293485515784412,0.0293485515784412,0.0293485515784412,0.0293485515784412,0.0058201058201058,0.0058201058201058,0.0058201058201058,0.0058201058201058,0.0058201058201058,0.0058201058201058,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105,0.1653439153439105]) / 6.
      elif degree == 8:
        coords = numeric.array([[0.2500000000000000,0.2500000000000000,0.2500000000000000],
                              [0.6175871903000830,0.1274709365666390,0.1274709365666390],
                              [0.1274709365666390,0.1274709365666390,0.1274709365666390],
                              [0.1274709365666390,0.1274709365666390,0.6175871903000830],
                              [0.1274709365666390,0.6175871903000830,0.1274709365666390],
                              [0.9037635088221031,0.0320788303926323,0.0320788303926323],
                              [0.0320788303926323,0.0320788303926323,0.0320788303926323],
                              [0.0320788303926323,0.0320788303926323,0.9037635088221031],
                              [0.0320788303926323,0.9037635088221031,0.0320788303926323],
                              [0.4502229043567190,0.0497770956432810,0.0497770956432810],
                              [0.0497770956432810,0.4502229043567190,0.0497770956432810],
                              [0.0497770956432810,0.0497770956432810,0.4502229043567190],
                              [0.0497770956432810,0.4502229043567190,0.4502229043567190],
                              [0.4502229043567190,0.0497770956432810,0.4502229043567190],
                              [0.4502229043567190,0.4502229043567190,0.0497770956432810],
                              [0.3162695526014501,0.1837304473985499,0.1837304473985499],
                              [0.1837304473985499,0.3162695526014501,0.1837304473985499],
                              [0.1837304473985499,0.1837304473985499,0.3162695526014501],
                              [0.1837304473985499,0.3162695526014501,0.3162695526014501],
                              [0.3162695526014501,0.1837304473985499,0.3162695526014501],
                              [0.3162695526014501,0.3162695526014501,0.1837304473985499],
                              [0.0229177878448171,0.2319010893971509,0.2319010893971509],
                              [0.2319010893971509,0.0229177878448171,0.2319010893971509],
                              [0.2319010893971509,0.2319010893971509,0.0229177878448171],
                              [0.5132800333608811,0.2319010893971509,0.2319010893971509],
                              [0.2319010893971509,0.5132800333608811,0.2319010893971509],
                              [0.2319010893971509,0.2319010893971509,0.5132800333608811],
                              [0.2319010893971509,0.0229177878448171,0.5132800333608811],
                              [0.0229177878448171,0.5132800333608811,0.2319010893971509],
                              [0.5132800333608811,0.2319010893971509,0.0229177878448171],
                              [0.2319010893971509,0.5132800333608811,0.0229177878448171],
                              [0.0229177878448171,0.2319010893971509,0.5132800333608811],
                              [0.5132800333608811,0.0229177878448171,0.2319010893971509],
                              [0.7303134278075384,0.0379700484718286,0.0379700484718286],
                              [0.0379700484718286,0.7303134278075384,0.0379700484718286],
                              [0.0379700484718286,0.0379700484718286,0.7303134278075384],
                              [0.1937464752488044,0.0379700484718286,0.0379700484718286],
                              [0.0379700484718286,0.1937464752488044,0.0379700484718286],
                              [0.0379700484718286,0.0379700484718286,0.1937464752488044],
                              [0.0379700484718286,0.7303134278075384,0.1937464752488044],
                              [0.7303134278075384,0.1937464752488044,0.0379700484718286],
                              [0.1937464752488044,0.0379700484718286,0.7303134278075384],
                              [0.0379700484718286,0.1937464752488044,0.7303134278075384],
                              [0.7303134278075384,0.0379700484718286,0.1937464752488044],
                              [0.1937464752488044,0.7303134278075384,0.0379700484718286]]).T
        weights = numeric.array([-0.2359620398477557,0.0244878963560562,0.0244878963560562,0.0244878963560562,0.0244878963560562,0.0039485206398261,0.0039485206398261,0.0039485206398261,0.0039485206398261,0.0263055529507371,0.0263055529507371,0.0263055529507371,0.0263055529507371,0.0263055529507371,0.0263055529507371,0.0829803830550589,0.0829803830550589,0.0829803830550589,0.0829803830550589,0.0829803830550589,0.0829803830550589,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0254426245481023,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852,0.0134324384376852]) / 6.
      else:
        NotImplementedError
    else:
      raise NotImplementedError
    return coords.T, weights

  def pointset_uniform( self, n ):
    if self.ndims == 1:
      return numeric.arange( .5, n )[:,_] / n, numeric.appendaxes( 1./n, n )
    elif self.ndims == 2:
      points = numeric.arange( 1./3, n ) / n
      nn = n**2
      C = numeric.empty( [2,n,n] )
      C[0] = points[:,_]
      C[1] = points[_,:]
      coords = C.reshape( 2, nn )
      flip = coords[0] + numeric.greater( coords[1], 1 )
      coords[:,flip] = 1 - coords[::-1,flip]
      weights = numeric.appendaxes( .5/nn, nn )
    else:
      raise NotImplementedError
    return coords.T, weights

  def pointset_bezier( self, np ):
    points = numeric.linspace( 0, 1, np )
    if self.ndims == 1:
      return points[:,_], None
    if self.ndims == 2:
      return numeric.array([ [x,y] for i, y in enumerate(points) for x in points[:np-i] ]), None
    raise NotImplementedError

  def pointset_vertex( self, n=0 ):
    return self.pointset_bezier( 2**n+1 )

class Tensor( Reference ):

  def __init__( self, simplex1, simplex2 ):
    self.simplex1 = simplex1
    self.simplex2 = simplex2
    ndims = simplex1.ndims + simplex2.ndims
    vertices = numeric.empty( ( simplex1.nverts, simplex2.nverts, ndims ), dtype=int )
    vertices[:,:,:simplex1.ndims] = simplex1.vertices[:,_]
    vertices[:,:,simplex1.ndims:] = simplex2.vertices[_,:]
    Reference.__init__( self, vertices.reshape(-1,ndims) )

    self._check_divergence()

  @cache.property
  def ribbon2vertices( self ):
    r2v1 = self.simplex1.ribbon2vertices[:,_,:] + self.simplex1.nverts * numeric.arange(self.simplex2.nverts)[_,:,_]
    r2v2 = self.simplex2.ribbon2vertices[:,_,:] * self.simplex1.nverts + numeric.arange(self.simplex1.nverts)[_,:,_]
    return numeric.concatenate([ r2v1.reshape(-1,2), r2v2.reshape(-1,2), ], axis=0 )

  def __str__( self ):
    return '%s*%s' % ( self.simplex1, self.simplex2 )

  def stdfunc( self, degree ):
    return self.simplex1.stdfunc(degree) * self.simplex2.stdfunc(degree)

  def pointset_vtk( self ):
    if self == Simplex(1)**2:
      points = [[0,0],[1,0],[1,1],[0,1]]
    elif self == Simplex(1)**3:
      points = [[0,0,0],[1,0,0],[0,1,0],[1,1,0],[0,0,1],[1,0,1],[0,1,1],[1,1,1]]
    else:
      raise NotImplementedError
    return numeric.array(points), numeric.ones(self.nverts)

  def pointset( self, pointset ):
    ipoints1, iweights1 = pointset( self.simplex1 )
    ipoints2, iweights2 = pointset( self.simplex2 )
    ipoints = numeric.empty( (ipoints1.shape[0],ipoints2.shape[0],self.ndims) )
    ipoints[:,:,0:self.simplex1.ndims] = ipoints1[:,_,:self.simplex1.ndims]
    ipoints[:,:,self.simplex1.ndims:self.ndims] = ipoints2[_,:,:self.simplex2.ndims]
    return ipoints.reshape( -1, self.ndims ), \
      iweights1 and iweights2 and ( iweights1[:,_] * iweights2[_,:] ).ravel()

  @cache.property
  def edges( self ):
    return [ ( transform.tensor( trans1, transform.Identity(self.simplex2.ndims) ), edge1 * self.simplex2 ) for trans1, edge1 in self.simplex1.edges ] \
         + [ ( transform.tensor( transform.Identity(self.simplex1.ndims), trans2 ), self.simplex1 * edge2 ) for trans2, edge2 in self.simplex2.edges ]

  @cache.property
  def children( self ):
    return [ ( transform.tensor(trans1,trans2), child1*child2 )
      for trans1, child1 in self.simplex1.children
        for trans2, child2 in self.simplex2.children ]

  @cache.propertylist
  def _child_subsets( self, level ):
    nverts1, subsets1 = self.simplex1._child_subsets[level]
    nverts2, subsets2 = self.simplex2._child_subsets[level]
    return nverts1 * nverts2, [ ( i1[:,_] * nverts2 + i2[_,:] ).ravel() for i1 in subsets1 for i2 in subsets2 ]


## STDELEMS


class StdElem( cache.Immutable ):
  'stdelem base class'

  __slots__ = 'ndims', 'nshapes'

  def __init__( self, ndims, nshapes ):
    self.ndims = ndims
    self.nshapes = nshapes

  def __mul__( self, other ):
    'multiply elements'

    return PolyProduct( self, other )

  def __pow__( self, n ):
    'repeated multiplication'

    assert n >= 1
    return self if n == 1 else self * self**(n-1)

  def extract( self, extraction ):
    'apply extraction matrix'

    return ExtractionWrapper( self, extraction )

class PolyProduct( StdElem ):
  'multiply standard elements'

  __slots__ = 'std',

  def __init__( self, *std ):
    'constructor'

    std1, std2 = self.std = std
    StdElem.__init__( self, ndims=std1.ndims+std2.ndims, nshapes=std1.nshapes*std2.nshapes )

  def eval( self, points, grad=0 ):
    'evaluate'
    # log.debug( '@ PolyProduct.eval: ', id(self), id(points), id(grad) )

    assert isinstance( grad, int ) and grad >= 0

    assert points.shape[-1] == self.ndims
    std1, std2 = self.std

    s1 = slice(0,std1.ndims)
    p1 = points[...,s1]
    s2 = slice(std1.ndims,None)
    p2 = points[...,s2]

    E = Ellipsis,
    S = slice(None),
    N = _,

    shape = points.shape[:-1] + (std1.nshapes * std2.nshapes,)
    G12 = [ ( std1.eval( p1, grad=i )[E+S+N+S*i+N*j]
            * std2.eval( p2, grad=j )[E+N+S+N*i+S*j] ).reshape( shape + (std1.ndims,) * i + (std2.ndims,) * j )
            for i,j in zip( range(grad,-1,-1), range(grad+1) ) ]

    data = numeric.empty( shape + (self.ndims,) * grad )

    s = (s1,)*grad + (s2,)*grad
    R = numeric.arange(grad)
    for n in range(2**grad):
      index = n>>R&1
      n = index.argsort() # index[s] = [0,...,1]
      shuffle = range(points.ndim) + list( points.ndim + n )
      iprod = index.sum()
      data.transpose(shuffle)[E+s[iprod:iprod+grad]] = G12[iprod]

    return data

  def __str__( self ):
    'string representation'

    return '%s*%s' % self.std

class PolyLine( StdElem ):
  'polynomial on a line'

  __slots__ = 'degree', 'poly'

  @classmethod
  def bernstein_poly( cls, degree ):
    'bernstein polynomial coefficients'

    # magic bernstein triangle
    revpoly = numeric.zeros( [degree+1,degree+1], dtype=int )
    for k in range(degree//2+1):
      revpoly[k,k] = root = (-1)**degree if k == 0 else ( revpoly[k-1,k] * (k*2-1-degree) ) / k
      for i in range(k+1,degree+1-k):
        revpoly[i,k] = revpoly[k,i] = root = ( root * (k+i-degree-1) ) / i
    return revpoly[::-1]

  @classmethod
  def spline_poly( cls, p, n ):
    'spline polynomial coefficients'

    assert p >= 0, 'invalid polynomial degree %d' % p
    if p == 0:
      assert n == -1
      return numeric.array( [[[1.]]] )

    assert 1 <= n < 2*p
    extractions = numeric.empty(( n, p+1, p+1 ))
    extractions[0] = numeric.eye( p+1 )
    for i in range( 1, n ):
      extractions[i] = numeric.eye( p+1 )
      for j in range( 2, p+1 ):
        for k in reversed( range( j, p+1 ) ):
          alpha = 1. / min( 2+k-j, n-i+1 )
          extractions[i-1,:,k] = alpha * extractions[i-1,:,k] + (1-alpha) * extractions[i-1,:,k-1]
        extractions[i,-j-1:-1,-j-1] = extractions[i-1,-j:,-1]

    poly = cls.bernstein_poly( p )
    return numeric.contract( extractions[:,_,:,:], poly[_,:,_,:], axis=-1 )

  @classmethod
  def spline_elems( cls, p, n ):
    'spline elements, minimum amount (just for caching)'

    return map( cls, cls.spline_poly(p,n) )

  @classmethod
  def spline_elems_neumann( cls, p, n ):
    'spline elements, neumann endings (just for caching)'

    polys = cls.spline_poly(p,n)
    poly_0 = polys[0].copy()
    poly_0[:,1] += poly_0[:,0]
    poly_e = polys[-1].copy()
    poly_e[:,-2] += poly_e[:,-1]
    return cls(poly_0), cls(poly_e)

  @classmethod
  def spline_elems_curvature( cls ):
    'spline elements, curve free endings (just for caching)'

    polys = cls.spline_poly(1,1)
    poly_0 = polys[0].copy()
    poly_0[:,0] += 0.5*(polys[0][:,0]+polys[0][:,1])
    poly_0[:,1] -= 0.5*(polys[0][:,0]+polys[0][:,1])

    poly_e = polys[-1].copy()
    poly_e[:,-2] -= 0.5*(polys[-1][:,-1]+polys[-1][:,-2])
    poly_e[:,-1] += 0.5*(polys[-1][:,-1]+polys[-1][:,-2])

    return cls(poly_0), cls(poly_e)

  @classmethod
  def spline( cls, degree, nelems, periodic=False, neumann=0, curvature=False ):
    'spline elements, any amount'

    p = degree
    n = 2*p-1
    if periodic:
      assert not neumann, 'periodic domains have no boundary'
      assert not curvature, 'curvature free option not possible for periodic domains'
      if nelems == 1: # periodicity on one element can only mean a constant
        elems = cls.spline_elems( 0, n )
      else:
        elems = cls.spline_elems( p, n )[p-1:p] * nelems
    else:
      elems = cls.spline_elems( p, min(nelems,n) )
      if len(elems) < nelems:
        elems = elems[:p-1] + elems[p-1:p] * (nelems-2*(p-1)) + elems[p:]
      if neumann:
        elem_0, elem_e = cls.spline_elems_neumann( p, min(nelems,n) )
        if neumann & 1:
          elems[0] = elem_0
        if neumann & 2:
          elems[-1] = elem_e
      if curvature:
        assert neumann==0, 'Curvature free not allowed in combindation with Neumann'
        assert degree==2, 'Curvature free only allowed for quadratic splines'  
        elem_0, elem_e = cls.spline_elems_curvature()
        elems[0] = elem_0
        elems[-1] = elem_e

    return numeric.array( elems )

  def __init__( self, poly ):
    '''Create polynomial from order x nfuncs array of coefficients 'poly'.
       Evaluates to sum_i poly[i,:] x**i.'''

    self.poly = numeric.asarray( poly, dtype=float )
    order, nshapes = self.poly.shape
    self.degree = order - 1
    StdElem.__init__( self, ndims=1, nshapes=nshapes )

  def eval( self, points, grad=0 ):
    'evaluate'

    assert points.shape[-1] == 1
    x = points[...,0]

    if grad > self.degree:
      return numeric.appendaxes( 0., x.shape+(self.nshapes,)+(1,)*grad )

    poly = self.poly
    for n in range(grad):
      poly = poly[1:] * numeric.arange( 1, poly.shape[0] )[:,_]

    polyval = numeric.empty( x.shape+(self.nshapes,) )
    polyval[:] = poly[-1]
    for p in poly[-2::-1]:
      polyval *= x[...,_]
      polyval += p

    return polyval[(Ellipsis,)+(_,)*grad]

  def extract( self, extraction ):
    'apply extraction'

    return PolyLine( numeric.dot( self.poly, extraction ) )

  def __repr__( self ):
    'string representation'

    return 'PolyLine#%x' % id(self)

class PolyTriangle( StdElem ):
  '''poly triangle (linear for now)
     conventions: dof numbering as vertices, see TriangularElement docstring.'''

  __slots__ = ()

  def __init__( self, degree ):
    'constructor'

    assert degree == 1, 'only linear implemented on triangles for now'
    StdElem.__init__( self, ndims=2, nshapes=3 )

  def eval( self, points, grad=0 ):
    'eval'

    npoints, ndim = points.shape
    if grad == 0:
      x, y = points.T
      data = numeric.array( [ 1-x-y, x, y ] ).T
    elif grad == 1:
      data = numeric.array( [[-1,-1],[1,0],[0,1]], dtype=float )
    else:
      data = numeric.array( 0 ).reshape( (1,) * (grad+ndim) )
    return data

  def __repr__( self ):
    'string representation'

    return '%s#%x' % ( self.__class__.__name__, id(self) )

class BubbleTriangle( StdElem ):
  '''linear triangle + bubble function
     conventions: dof numbering as vertices (see TriangularElement docstring), then barycenter.'''

  __slots__ = ()

  def __init__( self, order ):
    'constructor'

    assert order == 1
    StdElem.__init__( self, ndims=2, nshapes=4 )

  def eval( self, points, grad=0 ):
    'eval'

    npoints, ndim = points.shape
    if grad == 0:
      x, y = points.T
      data = numeric.array( [ x, y, 1-x-y, 27*x*y*(1-x-y) ] ).T
    elif grad == 1:
      x, y = points.T
      const_block = numeric.array( [1,0,0,1,-1,-1]*npoints, dtype=float ).reshape( npoints,3,2 )
      grad1_bubble = 27*numeric.array( [y*(1-2*x-y),x*(1-x-2*y)] ).T.reshape( npoints,1,2 )
      data = numeric.concatenate( [const_block, grad1_bubble], axis=1 )
    elif grad == 2:
      x, y = points.T
      zero_block = numeric.zeros( (npoints,3,2,2) )
      grad2_bubble = 27*numeric.array( [-2*y,1-2*x-2*y, 1-2*x-2*y,-2*x] ).T.reshape( npoints,1,2,2 )
      data = numeric.concatenate( [zero_block, grad2_bubble], axis=1 )
    elif grad == 3:
      zero_block = numeric.zeros( (3,2,2,2) )
      grad3_bubble = 27*numeric.array( [0,-2,-2,-2,-2,-2,-2,0], dtype=float ).reshape( 1,2,2,2 )
      data = numeric.concatenate( [zero_block, grad3_bubble], axis=0 )
    else:
      assert ndim==2, 'Triangle takes 2D coordinates' # otherwise tested by unpacking points.T
      data = numeric.array( 0 ).reshape( (1,) * (grad+2) )
    return data

  def __repr__( self ):
    'string representation'

    return '%s#%x' % ( self.__class__.__name__, id(self) )

class ExtractionWrapper( StdElem ):
  'extraction wrapper'

  __slots__ = 'stdelem', 'extraction'

  def __init__( self, stdelem, extraction ):
    'constructor'

    self.stdelem = stdelem
    assert extraction.shape[0] == stdelem.nshapes
    self.extraction = extraction
    StdElem.__init__( self, ndims=stdelem.ndims, nshapes=extraction.shape[1] )

  def eval( self, points, grad=0 ):
    'call'

    return numeric.dot( self.stdelem.eval( points, grad ), self.extraction, axis=1 )

  def __repr__( self ):
    'string representation'

    return '%s#%x:%s' % ( self.__class__.__name__, id(self), self.stdelem )


# vim:shiftwidth=2:foldmethod=indent:foldnestmax=2
