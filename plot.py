from . import topology, util, numpy, _

import matplotlib
matplotlib.use( 'Agg' )

import os, tempfile

class Pylab( object ):
  'matplotlib figure'

  def __init__( self, title ):
    'constructor'

    if isinstance( title, (list,tuple) ):
      self.title = numpy.array( title, dtype=object )
      self.shape = self.title.shape
      if self.title.ndim == 1:
        self.title = self.title[:,_]
      assert self.title.ndim == 2
    else:
      self.title = numpy.array( [[ title ]] )
      self.shape = ()

  def __enter__( self ):
    'enter with block'

    from matplotlib import pyplot
    pyplot.figure()
    n, m = self.title.shape
    axes = [ PylabAxis( pyplot.subplot(n,m,iax+1), title ) for iax, title in enumerate( self.title.ravel() ) ]
    return numpy.array( axes, dtype=object ).reshape( self.shape ) if self.shape else axes[0]

  def __exit__( self, exc, msg, tb ):
    'exit with block'

    if exc:
      print 'ERROR: plot failed:', msg or exc
      return #True

    from matplotlib import pyplot
    fileobj = tempfile.NamedTemporaryFile( dir=util.DUMPDIR, prefix='fig', suffix='.png', delete=False )
    path = fileobj.name
    name = os.path.basename( path )
    print 'saving to', name
    pyplot.savefig( fileobj )
    os.chmod( path, 0644 )
    pyplot.close()

class PylabAxis( object ):
  'matplotlib axis augmented with finity-specific functions'

  def __init__( self, ax, title ):
    'constructor'

    ax.set_title( title )
    self._ax = ax

  def __getattr__( self, attr ):
    'forward getattr to axis'

    return getattr( self._ax, attr )

  def add_mesh( self, coords, topology, color=None, edgecolors='none', linewidth=1, xmargin=0, ymargin=0, aspect='equal', cbar='vertical', title=None, ischeme='gauss2', cscheme='contour3', clim=None, frame=True ):
    'plot mesh'
  
    from matplotlib import pylab, collections
    poly = []
    values = []
    ndims, = coords.shape
    assert ndims in (2,3)
    for elem in util.progressbar( topology, title='plotting mesh' ):
      C = coords( elem.eval(cscheme) )
      if ndims == 3:
        C = project3d( C )
        cx, cy = numpy.hstack( [ C, C[:,:1] ] )
        if ( (cx[1:]-cx[:-1]) * (cy[1:]+cy[:-1]) ).sum() > 0:
          continue
      poly.append( C.T )
      if color is not None:
        xi = elem.eval(ischeme)
        values.append( util.mean( color(xi), weights=xi.weights ) )
  
    if values:
      elements = collections.PolyCollection( poly, edgecolors=edgecolors, linewidth=linewidth )
      elements.set_array( numpy.asarray(values) )
      if cbar:
        pylab.colorbar( elements, orientation=cbar )
    else:
      elements = collections.PolyCollection( poly, edgecolors='black', facecolors='none', linewidth=linewidth )
    if clim:
      elements.set_clim( *clim )
    if ndims == 3:
      self.get_xaxis().set_visible( False )
      self.get_yaxis().set_visible( False )
      self.box( 'off' )
    self.add_collection( elements )
    vertices = numpy.concatenate( poly )
    xmin, ymin = vertices.min(0)
    xmax, ymax = vertices.max(0)
    if not isinstance( xmargin, tuple ):
      xmargin = xmargin, xmargin
    self.set_xlim( xmin - xmargin[0], xmax + xmargin[1] )
    if not isinstance( ymargin, tuple ):
      ymargin = ymargin, ymargin
    self.set_ylim( ymin - ymargin[0], ymax + ymargin[1] )
    if aspect:
      self.set_aspect( aspect )
      self.set_autoscale_on( False )
    if title:
      self.title( title )
    self.set_frame_on( frame )
  
  def add_quiver( self, coords, topology, quiver, sample='uniform3' ):
    'quiver builder'
  
    XYUV = []
    for elem in util.progressbar( topology, title='plotting quiver' ):
      xi = elem.eval(sample)
      XYUV.append( numpy.concatenate( [ coords(xi), quiver(xi) ], axis=0 ) )
    return self.quiver( *numpy.concatenate( XYUV, 1 ) )

  def add_graph( self, coords, topology, function, linestyle, sample='contour10', **kwargs ):
    'plot graph of function on 1d topology'

    X = []
    Y = []
    for elem in topology:
      xi = elem.eval( sample )
      X.extend( coords( xi ).flatten() )
      X.append( numpy.nan )
      Y.extend( function( xi ) )
      Y.append( numpy.nan )
    p = self.plot( X, Y, linestyle, **kwargs )
    #self.set_xlim( min(X), max(X) )
    return p

def project3d( C ):
  sqrt2 = numpy.sqrt( 2 )
  sqrt3 = numpy.sqrt( 3 )
  sqrt6 = numpy.sqrt( 6 )
  R = numpy.array( [[ sqrt3, 0, -sqrt3 ], [ 1, 2, 1 ], [ sqrt2, -sqrt2, sqrt2 ]] ) / sqrt6
  return util.transform( C, R[:,::2], axis=0 )

def writevtu( path, topology, coords, **arrays ):
  'write vtu from coords function'

  import vtk
  vtkPoints = vtk.vtkPoints()
  vtkMesh = vtk.vtkUnstructuredGrid()
  vtkarrays = []
  for key, func in arrays.iteritems():
    array = vtk.vtkFloatArray()
    array.SetName( key )
    vtkarrays.append(( array, func ))
  for elem in util.progressbar( topology, title='saving %s' % path ):
    xi = elem.eval( 'contour0' )
    x = coords( xi )  
    id0, id1, id2 = [ vtkPoints.InsertNextPoint( *c ) for c in x.T ]
    triangle = vtk.vtkTriangle()
    cellpoints = triangle.GetPointIds()
    cellpoints.SetId( 0, id0 )
    cellpoints.SetId( 1, id1 )
    cellpoints.SetId( 2, id2 )
    vtkMesh.InsertNextCell( triangle.GetCellType(), cellpoints )
    for array, func in vtkarrays:
      for v in func( xi ):
        array.InsertNextValue( v )
  vtkMesh.SetPoints( vtkPoints )
  for array, func in vtkarrays:
    vtkMesh.GetPointData().AddArray( array )
  vtkWriter = vtk.vtkXMLUnstructuredGridWriter()
  vtkWriter.SetInput( vtkMesh )
  vtkWriter.SetFileName( path )
  vtkWriter.SetDataModeToAscii()
  vtkWriter.Write()

def preview( coords, topology, cscheme='contour8' ):
  'preview function'

  if topology.ndims == 3:
    topology = topology.boundary

  from matplotlib import pyplot, collections
  if coords.shape[0] == 2:
    mesh( coords, topology, cscheme=cscheme )
  elif coords.shape[0] == 3:
    polys = [ [] for i in range(4) ]
    for elem in topology:
      contour = coords( elem.eval(cscheme) )
      polys[0].append( project3d( contour ).T )
      polys[1].append( contour[:2].T )
      polys[2].append( contour[1:].T )
      polys[3].append( contour[::2].T )
    for iplt, poly in enumerate( polys ):
      elements = collections.PolyCollection( poly, edgecolors='black', facecolors='none', linewidth=1 )
      ax = pyplot.subplot( 2, 2, iplt+1 )
      ax.add_collection( elements )
      xmin, ymin = numpy.min( [ numpy.min(p,axis=0) for p in poly ], axis=0 )
      xmax, ymax = numpy.max( [ numpy.max(p,axis=0) for p in poly ], axis=0 )
      d = .02 * (xmax-xmin+ymax-ymin)
      pyplot.axis([ xmin-d, xmax+d, ymin-d, ymax+d ])
      if iplt == 0:
        ax.get_xaxis().set_visible( False )
        ax.get_yaxis().set_visible( False )
        pyplot.box( 'off' )
      else:
        pyplot.title( '?ZXY'[iplt] )
  else:
    raise Exception, 'need 2D or 3D coordinates'
  pyplot.show()

# OLD

def build_image( coords, n=3, extent=(0,1,0,1), ax=None, ticks=True, clim=None, cbar=False, title='plotting' ):
  'image builder'

  if ax is None:
    ax = gca()
  assert isinstance( coords.topology, topology.StructuredTopology )
  assert coords.topology.ndims == 2
  image = numpy.zeros( coords.topology.structure.shape + (n,n) )
  scheme = 'uniform%d' % n
  items = zip( coords.topology, image.reshape(-1,n,n) )
  if title:
    items = util.progressbar( items, title=title )
  for elem, im in items:
    yield coords( elem(scheme) ), im.ravel()
  image = image.swapaxes(1,2).reshape( image.shape[0]*n, image.shape[1]*n )
  im = ax.imshow( image.T, extent=extent, origin='lower' )
  if not ticks:
    ax.xaxis.set_ticks( [] )
    ax.yaxis.set_ticks( [] )
  if clim:
    im.set_clim( *clim )
  if cbar:
    colorbar( im, orientation='vertical' )

def plotmatr( A, **kwargs ):
  'Plot 10^log magnitudes of numpy matrix elements'

  A = numpy.log10( abs( A ) )
  if numpy.all( numpy.isinf( A ) ):
    A = numpy.zeros( A.shape )
  else:
    A[numpy.isinf( A )] = numpy.amin( A[~numpy.isinf( A )] ) - 1.
  pcolor( A, **kwargs )
  colorbar()
  ylim( ylim()[-1::-1] ) # invert y axis: equiv to MATLAB axis ij
  axis( 'tight' )

def savepdf( name, fig=None ):
  'save figure in plots dir'

  if fig is None:
    fig = gcf()
  path = os.path.join( 'plots', name + '.pdf' )
  dirname = os.path.dirname( path )
  if not os.path.isdir( dirname ):
    os.makedirs( dirname )
  fig.savefig( path, bbox_inches='tight', pad_inches=0 )

# vim:shiftwidth=2:foldmethod=indent:foldnestmax=2