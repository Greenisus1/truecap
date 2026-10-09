"""Fullscreen adapter for simple input/print Python utilities; CLI args stay unchanged."""
import builtins,curses,io,os,runpy,sys,textwrap,unicodedata
from pathlib import Path

def safe(value):return ''.join(c if c in '\n\t' or c.isprintable() and unicodedata.category(c)!='Cf' else '?' for c in str(value))
class UI:
 def __init__(self,s,title):self.s=s;self.title=title;self.lines=[];self.offset=0
 def add(self,value):
  self.lines.extend(safe(value).splitlines());self.lines=self.lines[-3000:];self.offset=0
 def paint(self,prompt='',buffer=''):
  s=self.s;h,w=s.getmaxyx();s.erase()
  def put(y,x,v,style=0):
   if 0<=y<h and 0<=x<w-1:
    try:s.addnstr(y,x,v,w-x-1,style)
    except curses.error:pass
  put(0,1,self.title+' - fullscreen',curses.A_BOLD)
  wrapped=[part for line in self.lines for part in textwrap.wrap(line,max(10,w-4),replace_whitespace=False) or ['']]
  count=max(1,h-5);self.offset=min(self.offset,max(0,len(wrapped)-count));end=len(wrapped)-self.offset
  for y,line in enumerate(wrapped[max(0,end-count):end],2):put(y,2,line)
  put(h-2,1,safe(prompt)+safe(buffer)[-max(1,w-len(safe(prompt))-3):],curses.A_REVERSE)
  put(h-1,1,'Enter submit | Backspace edit | PgUp/PgDn scroll | Ctrl+C cancel')
  s.refresh()
 def input(self,prompt=''):
  buffer='';self.paint(prompt,buffer)
  while True:
   try:k=self.s.get_wch()
   except curses.error:continue
   if k in (curses.KEY_PPAGE,curses.KEY_NPAGE):self.offset=max(0,self.offset+(10 if k==curses.KEY_PPAGE else -10));self.paint(prompt,buffer);continue
   if k in ('\n','\r'):self.add(prompt+buffer);return buffer
   if k=='\x03':raise KeyboardInterrupt
   if k in ('\x7f','\b',curses.KEY_BACKSPACE):buffer=buffer[:-1]
   elif isinstance(k,str) and k.isprintable() and len(buffer)<8192:buffer+=k
   self.paint(prompt,buffer)
 def review(self):
  self.paint('Result - Enter returns, Ctrl+C closes')
  while True:
   k=self.s.getch()
   if k in (10,13):return
   if k==3:raise KeyboardInterrupt
   if k in (curses.KEY_PPAGE,curses.KEY_NPAGE):self.offset=max(0,self.offset+(10 if k==curses.KEY_PPAGE else -10));self.paint('Result - Enter returns, Ctrl+C closes')
class Output:
 def __init__(self,ui):self.ui=ui;self.partial=''
 def write(self,value):
  self.partial+=str(value)
  while '\n' in self.partial:line,self.partial=self.partial.split('\n',1);self.ui.add(line)
  return len(value)
 def flush(self):
  self.ui.paint()
 def isatty(self):return True

def launch(script,title):
 root=Path(__file__).resolve().parent;script=root/script
 if len(sys.argv)>1 or not sys.stdin.isatty() or not sys.stdout.isatty():sys.argv[0]=str(script);runpy.run_path(str(script),run_name='__main__');return
 def session(s):
  s.keypad(True)
  try:curses.curs_set(0)
  except curses.error:pass
  ui=UI(s,title);out=Output(ui);code=0;original=(builtins.input,sys.stdout,sys.stderr,sys.argv[:])
  try:
   def ask(prompt=''):
    if out.partial:prompt=out.partial+prompt;out.partial=''
    return ui.input(prompt)
   builtins.input=ask;sys.stdout=sys.stderr=out;sys.argv=[str(script)]
   try:runpy.run_path(str(script),run_name='__main__')
   except SystemExit as e:
    code=e.code if isinstance(e.code,int) else 0 if e.code is None else 1
    if e.code not in (None,0):ui.add('Exit status: '+str(e.code))
   if out.partial:ui.add(out.partial)
   ui.review();return code
  finally:builtins.input,sys.stdout,sys.stderr,sys.argv=original
 try:return curses.wrapper(session)
 except KeyboardInterrupt:return
