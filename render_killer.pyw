"""Render Killer: lists Gaffer background renders and kick and prman renders, and kills them.

Gaffer's LocalDispatcher runs each background task as its own
`gaffer execute` process tree. Killing that tree fails the task, and the
dispatcher then stops the rest of the job, the same as cancelling it in
Gaffer's Local Jobs panel.

Gaffer renders Arnold and RenderMan inside its own process, so the renderer
is identified from the DLL it has loaded (`ai.dll` or `libprman.dll`).

Standard library only. Process info comes from PowerShell's CIM query.
"""

import base64
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

REFRESH_SECONDS = 2.0
CPU_COUNT = os.cpu_count() or 1
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

RENDERER_DLLS = { "ai.dll" : "Arnold", "libprman.dll" : "RenderMan" }

# Module lists are only read for Gaffer render processes, because reading
# them for every process would make each scan slow.
PS_QUERY = r"""
$procs = Get-CimInstance Win32_Process
$modules = @{}
foreach ( $p in $procs | Where-Object { $_.Name -eq 'gaffer.exe' -and $_.CommandLine -match '\s(execute|python|dispatch)\s' } ) {
	try {
		$modules["$($p.ProcessId)"] = @( ( Get-Process -Id $p.ProcessId -ErrorAction Stop ).Modules |
			Where-Object { $_.ModuleName -match '^(ai|libprman)\.dll$' } | ForEach-Object { $_.ModuleName.ToLower() } )
	} catch {}
}
@{
	procs = @( $procs | Select-Object ProcessId,ParentProcessId,Name,CommandLine,CreationDate,KernelModeTime,UserModeTime,WorkingSetSize )
	modules = $modules
} | ConvertTo-Json -Compress -Depth 3
"""

BG = "#2b2b2b"
PANEL = "#373737"
FIELD = "#232323"
TEXT = "#d6d6d6"
DIM = "#8a8a8a"
ACCENT = "#7fb6de"
DANGER = "#c4554d"


# Process discovery
# -----------------

def queryProcesses() :

	out = subprocess.run(
		# -EncodedCommand avoids command-line quoting problems with the multi-line script.
		[ "powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode( PS_QUERY.encode( "utf-16-le" ) ).decode() ],
		capture_output = True, text = True, creationflags = NO_WINDOW,
	).stdout
	data = json.loads( out or "{}" )
	modules = data.get( "modules" ) or {}
	procs = {}
	for p in data.get( "procs" ) or [] :
		created = p.get( "CreationDate" )
		if isinstance( created, str ) :
			m = re.search( r"\d+", created )
			created = int( m.group() ) / 1000.0 if m else None
		elif isinstance( created, dict ) :
			created = None
		procs[p["ProcessId"]] = {
			"pid" : p["ProcessId"],
			"ppid" : p["ParentProcessId"],
			"name" : ( p.get( "Name" ) or "" ).lower(),
			"cmd" : p.get( "CommandLine" ) or "",
			"created" : created,
			# CIM times are in 100ns units.
			"cpu" : ( ( p.get( "KernelModeTime" ) or 0 ) + ( p.get( "UserModeTime" ) or 0 ) ) / 1e7,
			"mem" : p.get( "WorkingSetSize" ) or 0,
			"modules" : modules.get( str( p["ProcessId"] ) ) or [],
		}
	return procs

GAFFER_APPS = ( "execute", "python", "dispatch" )

def gafferApp( p ) :

	"""Returns the Gaffer app a process is running (`execute` for dispatcher
	tasks, `python` or `dispatch` for scripted renders), or None. The GUI has
	no app argument, so it never matches."""

	# Each launcher stage must be running Gaffer itself, not just mention it in
	# its arguments: `cmd /c gaffer.cmd`, `python _gaffer.py`, or `gaffer.exe`.
	launcher = {
		"cmd.exe" : r"^\S*cmd(?:\.exe)?\"?\s+/[cC]\s+\"?[^\"\s]*gaffer\.cmd",
		"python.exe" : r"^\"?[^\"]*\"?[^\"\s]*python\.exe\"?\s+\"?[^\"]*\"?[^\"\s]*_gaffer\.py",
		"gaffer.exe" : r"^\"?[^\"\s]*gaffer\.exe",
	}.get( p["name"] )
	if launcher is None :
		return None
	m = re.match( launcher + r"\"?\s+(" + "|".join( GAFFER_APPS ) + r")\s", p["cmd"], re.IGNORECASE )
	return m.group( 1 ).lower() if m else None

def isGafferExecute( p ) :

	return gafferApp( p ) is not None

# Standalone renderers, with the scene file extensions they take.
STANDALONE = {
	"kick.exe" : ( "kick", ( ".ass", ".ass.gz", ".usd", ".usda", ".usdc" ) ),
	"prman.exe" : ( "prman", ( ".rib", ".rib.gz", ".usd", ".usda", ".usdc" ) ),
}

def isStandalone( p ) :

	return p["name"] in STANDALONE

def isRender( p ) :

	return isGafferExecute( p ) or isStandalone( p )

def descendants( procs, pid ) :

	children = {}
	for p in procs.values() :
		children.setdefault( p["ppid"], [] ).append( p["pid"] )
	result, stack = [], [ pid ]
	while stack :
		current = stack.pop()
		for child in children.get( current, [] ) :
			if child != current and child not in result :
				result.append( child )
				stack.append( child )
	return result

def splitArgs( cmd ) :

	# Non-POSIX mode keeps Windows backslashes; it leaves quotes on, so strip them.
	try :
		args = shlex.split( cmd, posix = False )
	except ValueError :
		args = cmd.split()
	return [ a[1:-1] if len( a ) > 1 and a[0] == a[-1] and a[0] in "'\"" else a for a in args ]

def describeGaffer( cmd ) :

	args = splitArgs( cmd )
	info = { "script" : "", "node" : "", "frames" : "", "context" : [], "jobDir" : "" }
	i = 0
	inContext = False
	while i < len( args ) :
		a = args[i]
		value = args[i + 1] if i + 1 < len( args ) else ""
		if a == "-context" :
			inContext = True
		elif inContext and a.startswith( "-" ) :
			name = a[1:]
			if name == "dispatcher:jobDirectory" :
				info["jobDir"] = value.strip( "'\"" )
			elif not name.startswith( "dispatcher:" ) :
				info["context"].append( "{}={}".format( name, value.strip( "'\"" ) ) )
			i += 1
		elif a == "-script" :
			info["script"] = value
			i += 1
		elif a == "-nodes" :
			info["node"] = value
			i += 1
		elif a == "-frames" :
			info["frames"] = value
			i += 1
		i += 1

	if not info["jobDir"] and info["script"] :
		info["jobDir"] = os.path.dirname( info["script"] )
	return info

def describeGafferScript( cmd, app ) :

	# e.g. `gaffer.exe python render_all.py scene.gfr NodeA NodeB`
	args = splitArgs( cmd )
	lowered = [ a.lower() for a in args ]
	rest = args[lowered.index( app ) + 1:] if app in lowered else []
	script = next( ( a for a in rest if a.lower().endswith( ".py" ) ), "" )
	scene = next( ( a for a in rest if a.lower().endswith( ".gfr" ) ), "" )
	others = [ a for a in rest if a not in ( script, scene ) ]
	job = " ".join( os.path.splitext( os.path.basename( f ) )[0] for f in ( script, scene ) if f )
	return {
		"script" : script or scene, "job" : job or "gaffer " + app, "node" : "", "frames" : "",
		"context" : others, "jobDir" : os.path.dirname( scene or script ),
	}

def describeStandalone( cmd, extensions ) :

	args = splitArgs( cmd )
	scene = next( ( a for a in args[1:] if a.lower().endswith( extensions ) ), "" )
	return {
		"script" : scene, "node" : "", "frames" : "", "context" : [],
		"jobDir" : os.path.dirname( scene ) if scene else "",
	}

def findRenders( procs ) :

	renders = []
	for p in procs.values() :
		if not isRender( p ) :
			continue
		parent = procs.get( p["ppid"] )
		if parent is not None and isRender( parent ) and parent["pid"] != p["pid"] :
			continue

		tree = [ p["pid"] ] + descendants( procs, p["pid"] )
		members = [ procs[pid] for pid in tree if pid in procs ]
		gafferProcs = [ m for m in members if isGafferExecute( m ) ]
		gafferCmds = [ m["cmd"] for m in gafferProcs ]

		if gafferCmds :
			# The innermost process has the cleanest command line.
			app = gafferApp( gafferProcs[-1] )
			if app == "execute" :
				info = describeGaffer( gafferCmds[-1] )
				kind = "Gaffer"
				job = os.path.splitext( os.path.basename( info["script"] ) )[0]
				if "dispatcher" in info["jobDir"] :
					# LocalDispatcher job folders are numbered, e.g. .../Lookdev/000021.
					job += " " + os.path.basename( info["jobDir"].rstrip( "/\\" ) )
			else :
				info = describeGafferScript( gafferCmds[-1], app )
				kind = "Gaffer " + app
				job = info["job"]
			renderers = sorted( { RENDERER_DLLS[d] for m in gafferProcs for d in m["modules"] if d in RENDERER_DLLS } )
			if renderers :
				kind += " ({})".format( ", ".join( renderers ) )
			for name in sorted( { m["name"] for m in members if isStandalone( m ) } ) :
				kind += " + " + STANDALONE[name][0]
		else :
			label, extensions = STANDALONE[p["name"]]
			info = describeStandalone( p["cmd"], extensions )
			kind = label
			job = os.path.basename( info["script"] ) or label

		renders.append( {
			"pid" : p["pid"],
			"created" : p["created"],
			"tree" : tree,
			"kind" : kind,
			"job" : job,
			"node" : info["node"],
			"frames" : info["frames"],
			"context" : "  ".join( info["context"] ),
			"jobDir" : info["jobDir"],
			"cpu" : sum( m["cpu"] for m in members ),
			"mem" : sum( m["mem"] for m in members ),
			"cmd" : gafferCmds[-1] if gafferCmds else p["cmd"],
		} )

	renders.sort( key = lambda r : r["created"] or 0 )
	return renders

def killTree( pid ) :

	result = subprocess.run(
		[ "taskkill", "/PID", str( pid ), "/T", "/F" ],
		capture_output = True, text = True, creationflags = NO_WINDOW,
	)
	return result.returncode == 0, ( result.stdout + result.stderr ).strip()


# Formatting
# ----------

def formatElapsed( created ) :

	if not created :
		return ""
	seconds = int( time.time() - created )
	return str( datetime.timedelta( seconds = max( seconds, 0 ) ) )

def formatMem( b ) :

	return "{:.1f} GB".format( b / 1024 ** 3 ) if b >= 1024 ** 3 else "{:.0f} MB".format( b / 1024 ** 2 )


# UI
# --

class App( tk.Tk ) :

	COLUMNS = (
		( "kind", "Type", 100 ),
		( "job", "Job", 170 ),
		( "node", "Node", 110 ),
		( "frames", "Frames", 70 ),
		( "context", "Context", 300 ),
		( "elapsed", "Elapsed", 80 ),
		( "cpu", "CPU", 60 ),
		( "mem", "Memory", 80 ),
		( "pid", "PID", 70 ),
	)

	def __init__( self ) :

		tk.Tk.__init__( self )
		self.title( "Render Killer" )
		self.geometry( "1100x360" )
		self.minsize( 700, 220 )
		self.configure( bg = BG )
		# PyInstaller unpacks bundled files to sys._MEIPASS.
		icon = os.path.join( getattr( sys, "_MEIPASS", os.path.dirname( os.path.abspath( __file__ ) ) ), "icon.ico" )
		if os.path.exists( icon ) :
			self.iconbitmap( icon )

		self.renders = {}
		self.lastCpu = {}
		self.lastTime = None
		self.closing = False

		self.__buildStyle()
		self.__buildWidgets()
		self.__startPolling()

	def __buildStyle( self ) :

		style = ttk.Style( self )
		style.theme_use( "clam" )
		font = ( "Segoe UI", 9 )
		style.configure( ".", background = BG, foreground = TEXT, font = font )
		style.configure(
			"Treeview", background = FIELD, fieldbackground = FIELD, foreground = TEXT,
			rowheight = 24, borderwidth = 0,
		)
		style.map( "Treeview", background = [ ( "selected", "#4a5866" ) ], foreground = [ ( "selected", "#ffffff" ) ] )
		style.configure(
			"Treeview.Heading", background = PANEL, foreground = DIM, relief = "flat",
			font = ( "Segoe UI", 8, "bold" ), padding = ( 6, 4 ),
		)
		style.map( "Treeview.Heading", background = [ ( "active", "#414141" ) ] )
		style.configure( "TButton", background = PANEL, foreground = TEXT, borderwidth = 0, padding = ( 12, 6 ), focuscolor = PANEL )
		style.map( "TButton", background = [ ( "active", "#454545" ), ( "disabled", BG ) ], foreground = [ ( "disabled", "#5a5a5a" ) ] )
		style.configure( "Kill.TButton", background = "#5a2f2c", foreground = "#f0d0cc" )
		style.map( "Kill.TButton", background = [ ( "active", DANGER ), ( "disabled", BG ) ], foreground = [ ( "disabled", "#5a5a5a" ) ] )
		style.configure( "Status.TLabel", background = BG, foreground = DIM, font = ( "Segoe UI", 8 ) )
		style.configure( "Title.TLabel", background = BG, foreground = ACCENT, font = ( "Segoe UI", 11, "bold" ) )
		style.configure( "Vertical.TScrollbar", background = PANEL, troughcolor = FIELD, borderwidth = 0, arrowcolor = DIM )

	def __buildWidgets( self ) :

		header = ttk.Frame( self, padding = ( 12, 10, 12, 6 ) )
		header.pack( fill = "x" )
		ttk.Label( header, text = "Background renders", style = "Title.TLabel" ).pack( side = "left" )
		self.countLabel = ttk.Label( header, text = "", style = "Status.TLabel" )
		self.countLabel.pack( side = "left", padx = 10 )

		body = ttk.Frame( self, padding = ( 12, 0 ) )
		body.pack( fill = "both", expand = True )
		self.tree = ttk.Treeview( body, columns = [ c[0] for c in self.COLUMNS ], show = "headings", selectmode = "extended" )
		for key, label, width in self.COLUMNS :
			self.tree.heading( key, text = label, anchor = "w" )
			stretch = key in ( "context", "job" )
			self.tree.column( key, width = width, minwidth = 40, stretch = stretch, anchor = "w" )
		scroll = ttk.Scrollbar( body, orient = "vertical", command = self.tree.yview )
		self.tree.configure( yscrollcommand = scroll.set )
		self.tree.pack( side = "left", fill = "both", expand = True )
		scroll.pack( side = "right", fill = "y" )
		self.tree.bind( "<<TreeviewSelect>>", lambda e : self.__updateButtons() )
		self.tree.bind( "<Double-1>", lambda e : self.__openJobDir() )
		self.tree.bind( "<Delete>", lambda e : self.__killSelected() )

		footer = ttk.Frame( self, padding = ( 12, 8, 12, 10 ) )
		footer.pack( fill = "x" )
		self.killButton = ttk.Button( footer, text = "Kill selected", style = "Kill.TButton", command = self.__killSelected )
		self.killButton.pack( side = "left" )
		self.killAllButton = ttk.Button( footer, text = "Kill all", style = "Kill.TButton", command = self.__killAll )
		self.killAllButton.pack( side = "left", padx = 6 )
		self.openButton = ttk.Button( footer, text = "Open job folder", command = self.__openJobDir )
		self.openButton.pack( side = "left", padx = ( 12, 0 ) )
		self.copyButton = ttk.Button( footer, text = "Copy command", command = self.__copyCommand )
		self.copyButton.pack( side = "left", padx = 6 )
		self.status = ttk.Label( footer, text = "Scanning...", style = "Status.TLabel" )
		self.status.pack( side = "right" )

		self.empty = tk.Label(
			self.tree, text = "No background renders running", bg = FIELD, fg = "#5f5f5f",
			font = ( "Segoe UI", 10 ),
		)

		self.__updateButtons()

	# Polling runs on a thread so the slow PowerShell query never blocks the UI.

	def __startPolling( self ) :

		def poll() :
			while not self.closing :
				try :
					procs = queryProcesses()
					renders = findRenders( procs )
					error = None
				except Exception as e :
					renders, error = None, str( e )
				if self.closing :
					return
				self.after( 0, self.__applyResults, renders, error )
				time.sleep( REFRESH_SECONDS )

		threading.Thread( target = poll, daemon = True ).start()

	def __applyResults( self, renders, error ) :

		if error is not None :
			self.status.configure( text = "Scan failed: " + error )
			return

		now = time.time()
		dt = ( now - self.lastTime ) if self.lastTime else None
		self.lastTime = now

		current = {}
		for r in renders :
			key = ( r["pid"], r["created"] )
			previousCpu = self.lastCpu.get( key )
			if dt and previousCpu is not None :
				r["cpuPercent"] = max( 0.0, ( r["cpu"] - previousCpu ) / dt / CPU_COUNT * 100.0 )
			else :
				r["cpuPercent"] = None
			current[str( r["pid"] )] = r

		self.lastCpu = { ( r["pid"], r["created"] ) : r["cpu"] for r in renders }

		for iid in self.tree.get_children() :
			if iid not in current :
				self.tree.delete( iid )
		for iid, r in current.items() :
			values = (
				r["kind"], r["job"], r["node"], r["frames"], r["context"],
				formatElapsed( r["created"] ),
				"" if r["cpuPercent"] is None else "{:.0f}%".format( r["cpuPercent"] ),
				formatMem( r["mem"] ), r["pid"],
			)
			if self.tree.exists( iid ) :
				self.tree.item( iid, values = values )
			else :
				self.tree.insert( "", "end", iid = iid, values = values )

		self.renders = current
		count = len( current )
		self.countLabel.configure( text = "{} running".format( count ) if count else "" )
		if count :
			self.empty.place_forget()
		else :
			self.empty.place( relx = 0.5, rely = 0.55, anchor = "center" )
		self.status.configure( text = "Updated " + time.strftime( "%H:%M:%S" ) )
		self.__updateButtons()

	def __selected( self ) :

		return [ self.renders[iid] for iid in self.tree.selection() if iid in self.renders ]

	def __updateButtons( self ) :

		selected = self.__selected()
		self.killButton.state( [ "!disabled" ] if selected else [ "disabled" ] )
		self.killAllButton.state( [ "!disabled" ] if self.renders else [ "disabled" ] )
		self.openButton.state( [ "!disabled" ] if len( selected ) == 1 and selected[0]["jobDir"] else [ "disabled" ] )
		self.copyButton.state( [ "!disabled" ] if len( selected ) == 1 else [ "disabled" ] )

	def __killSelected( self ) :

		self.__kill( self.__selected() )

	def __killAll( self ) :

		self.__kill( list( self.renders.values() ) )

	def __kill( self, renders ) :

		if not renders :
			return

		lines = "\n".join(
			"  {}  {}  {}".format( r["job"], r["node"], r["context"] ).rstrip() for r in renders[:8]
		)
		if len( renders ) > 8 :
			lines += "\n  and {} more".format( len( renders ) - 8 )
		if not messagebox.askyesno(
			"Kill renders",
			"Kill {} render{}?\n\n{}{}".format(
				len( renders ), "" if len( renders ) == 1 else "s", lines,
				"\n\nGaffer marks dispatched jobs as failed and stops their remaining tasks."
				if any( r["kind"].startswith( "Gaffer" ) and r["node"] for r in renders ) else "",
			),
			icon = "warning", parent = self,
		) :
			return

		# Re-check each process right before killing it, in case Windows has
		# reused the PID for something else since the last scan.
		procs = queryProcesses()
		failures = []
		for r in renders :
			p = procs.get( r["pid"] )
			if p is None :
				continue
			if p["created"] != r["created"] or not isRender( p ) :
				failures.append( "{}: process changed, not killed".format( r["pid"] ) )
				continue
			ok, message = killTree( r["pid"] )
			if not ok :
				failures.append( "{}: {}".format( r["pid"], message ) )

		if failures :
			messagebox.showerror( "Kill renders", "\n".join( failures ), parent = self )
		self.status.configure( text = "Killed {} render{}".format(
			len( renders ) - len( failures ), "" if len( renders ) - len( failures ) == 1 else "s"
		) )

	def __openJobDir( self ) :

		selected = self.__selected()
		if len( selected ) == 1 and selected[0]["jobDir"] and os.path.isdir( selected[0]["jobDir"] ) :
			os.startfile( os.path.normpath( selected[0]["jobDir"] ) )

	def __copyCommand( self ) :

		selected = self.__selected()
		if len( selected ) == 1 :
			self.clipboard_clear()
			self.clipboard_append( selected[0]["cmd"] )
			self.status.configure( text = "Command copied" )

	def destroy( self ) :

		self.closing = True
		tk.Tk.destroy( self )


if __name__ == "__main__" :

	App().mainloop()
