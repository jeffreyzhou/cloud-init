# vi: ts=4 expandtab
#
#    Copyright (C) 2009-2010 Canonical Ltd.
#
#    Author: Scott Moser <scott.moser@canonical.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License version 3, as
#    published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
import yaml
import os
import os.path
import errno
import subprocess
from Cheetah.Template import Template
import urllib2
import urllib
import logging
import re
import socket
import sys
import time
import traceback
import urlparse

try:
    import selinux
    HAVE_LIBSELINUX = True
except ImportError:
    HAVE_LIBSELINUX = False

def read_conf(fname):
    try:
        stream = open(fname,"r")
        conf = yaml.load(stream)
        stream.close()
        return conf
    except IOError as e:
        if e.errno == errno.ENOENT:
            return { }
        raise

def get_base_cfg(cfgfile,cfg_builtin="", parsed_cfgs=None):
    kerncfg = { }
    syscfg = { }
    if parsed_cfgs and cfgfile in parsed_cfgs:
        return(parsed_cfgs[cfgfile])

    syscfg = read_conf_with_confd(cfgfile)

    kern_contents = read_cc_from_cmdline()
    if kern_contents:
        kerncfg = yaml.load(kern_contents)

    # kernel parameters override system config
    combined = mergedict(kerncfg, syscfg)

    if cfg_builtin:
        builtin = yaml.load(cfg_builtin)
        fin = mergedict(combined,builtin)
    else:
        fin = combined

    if parsed_cfgs != None:
        parsed_cfgs[cfgfile] = fin
    return(fin)

def get_cfg_option_bool(yobj, key, default=False):
    if not yobj.has_key(key): return default
    val = yobj[key]
    if val is True: return True
    if str(val).lower() in [ 'true', '1', 'on', 'yes']:
        return True
    return False

def get_cfg_option_str(yobj, key, default=None):
    if not yobj.has_key(key): return default
    return yobj[key]

def get_cfg_option_list_or_str(yobj, key, default=None):
    if not yobj.has_key(key): return default
    if yobj[key] is None: return []
    if isinstance(yobj[key],list): return yobj[key]
    return([yobj[key]])

# get a cfg entry by its path array
# for f['a']['b']: get_cfg_by_path(mycfg,('a','b'))
def get_cfg_by_path(yobj,keyp,default=None):
    cur = yobj
    for tok in keyp:
        if tok not in cur: return(default)
        cur = cur[tok]
    return(cur)

# merge values from cand into source
# if src has a key, cand will not override
def mergedict(src,cand):
    if isinstance(src,dict) and isinstance(cand,dict):
        for k,v in cand.iteritems():
            if k not in src:
                src[k] = v
            else:
                src[k] = mergedict(src[k],v)
    return src

def write_file(file,content,mode=0644,omode="wb"):
    try:
        os.makedirs(os.path.dirname(file))
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise e

    f=open(file,omode)
    if mode != None:
        os.chmod(file,mode)
    f.write(content)
    f.close()
    restorecon_if_possible(file)

def restorecon_if_possible(path, recursive=False):
    if HAVE_LIBSELINUX and selinux.is_selinux_enabled():
        selinux.restorecon(path, recursive=recursive)

# get keyid from keyserver
def getkeybyid(keyid,keyserver):
    shcmd="""
    k=${1} ks=${2};
    exec 2>/dev/null
    [ -n "$k" ] || exit 1;
    armour=$(gpg --list-keys --armour "${k}")
    if [ -z "${armour}" ]; then
       gpg --keyserver ${ks} --recv $k >/dev/null &&
          armour=$(gpg --export --armour "${k}") &&
          gpg --batch --yes --delete-keys "${k}"
    fi
    [ -n "${armour}" ] && echo "${armour}"
    """
    args=['sh', '-c', shcmd, "export-gpg-keyid", keyid, keyserver]
    return(subp(args)[0])

def runparts(dirp, skip_no_exist=True):
    if skip_no_exist and not os.path.isdir(dirp): return
        
    # per bug 857926, Fedora's run-parts will exit failure on empty dir
    if os.path.isdir(dirp) and os.listdir(dirp) == []: return

    cmd = [ 'run-parts', '--regex', '.*', dirp ]
    sp = subprocess.Popen(cmd)
    sp.communicate()
    if sp.returncode is not 0:
        raise subprocess.CalledProcessError(sp.returncode,cmd)
    return

def subp(args, input=None):
    sp = subprocess.Popen(args, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    out,err = sp.communicate(input)
    if sp.returncode is not 0:
        raise subprocess.CalledProcessError(sp.returncode,args, (out,err))
    return(out,err)

def render_to_file(template, outfile, searchList):
    t = Template(file='/etc/cloud/templates/%s.tmpl' % template, searchList=[searchList])
    f = open(outfile, 'w')
    f.write(t.respond())
    f.close()

def render_string(template, searchList):
    return(Template(template, searchList=[searchList]).respond())


# read_optional_seed
# returns boolean indicating success or failure (presense of files)
# if files are present, populates 'fill' dictionary with 'user-data' and
# 'meta-data' entries
def read_optional_seed(fill,base="",ext="", timeout=5):
    try:
        (md,ud) = read_seeded(base,ext,timeout)
        fill['user-data']= ud
        fill['meta-data']= md
        return True
    except OSError, e:
        if e.errno == errno.ENOENT:
            return False
        raise
    

# raise OSError with enoent if not found
def read_seeded(base="", ext="", timeout=5, retries=10, file_retries=0):
    if base.startswith("/"):
        base="file://%s" % base

    # default retries for file is 0. for network is 10
    if base.startswith("file://"):
        retries = file_retries

    if base.find("%s") >= 0:
        ud_url = base % ("user-data" + ext)
        md_url = base % ("meta-data" + ext)
    else:
        ud_url = "%s%s%s" % (base, "user-data", ext)
        md_url = "%s%s%s" % (base, "meta-data", ext)

    raise_err = None
    for attempt in range(0,retries+1):
        try:
            md_str = readurl(md_url, timeout=timeout)
            ud = readurl(ud_url, timeout=timeout)
            md = yaml.load(md_str)
    
            return(md,ud)
        except urllib2.HTTPError as e:
            raise_err = e
        except urllib2.URLError as e:
            raise_err = e
            if isinstance(e.reason,OSError) and e.reason.errno == errno.ENOENT:
                raise_err = e.reason 

        if attempt == retries:
            break

        #print "%s failed, sleeping" % attempt
        time.sleep(1)

    raise(raise_err)

def logexc(log,lvl=logging.DEBUG):
    log.log(lvl,traceback.format_exc())

class RecursiveInclude(Exception):
    pass

def read_file_with_includes(fname, rel = ".", stack=[], patt = None):
    if not fname.startswith("/"):
        fname = os.sep.join((rel, fname))

    fname = os.path.realpath(fname)

    if fname in stack:
        raise(RecursiveInclude("%s recursively included" % fname))
    if len(stack) > 10:
        raise(RecursiveInclude("%s included, stack size = %i" %
                               (fname, len(stack))))

    if patt == None:
        patt = re.compile("^#(opt_include|include)[ \t].*$",re.MULTILINE)

    try:
        fp = open(fname)
        contents = fp.read()
        fp.close()
    except:
        raise

    rel = os.path.dirname(fname)
    stack.append(fname)

    cur = 0
    while True:
        match = patt.search(contents[cur:])
        if not match: break
        loc = match.start() + cur
        endl = match.end() + cur

        (key, cur_fname) = contents[loc:endl].split(None,2)
        cur_fname = cur_fname.strip()

        try:
            inc_contents = read_file_with_includes(cur_fname, rel, stack, patt)
        except IOError, e:
            if e.errno == errno.ENOENT and key == "#opt_include":
                inc_contents = ""
            else:
                raise
        contents = contents[0:loc] + inc_contents + contents[endl+1:]
        cur = loc + len(inc_contents)
    stack.pop()
    return(contents)

def read_conf_d(confd):
    # get reverse sorted list (later trumps newer)
    confs = sorted(os.listdir(confd),reverse=True)
    
    # remove anything not ending in '.cfg'
    confs = filter(lambda f: f.endswith(".cfg"), confs)

    # remove anything not a file
    confs = filter(lambda f: os.path.isfile("%s/%s" % (confd,f)),confs)

    cfg = { }
    for conf in confs:
        cfg = mergedict(cfg,read_conf("%s/%s" % (confd,conf)))

    return(cfg)

def read_conf_with_confd(cfgfile):
    cfg = read_conf(cfgfile)
    confd = False
    if "conf_d" in cfg:
        if cfg['conf_d'] is not None:
            confd = cfg['conf_d']
            if not isinstance(confd,str):
                raise Exception("cfgfile %s contains 'conf_d' with non-string" % cfgfile)
    elif os.path.isdir("%s.d" % cfgfile):
        confd = "%s.d" % cfgfile

    if not confd: return(cfg)

    confd_cfg = read_conf_d(confd)

    return(mergedict(confd_cfg,cfg))


def get_cmdline():
    if 'DEBUG_PROC_CMDLINE' in os.environ:
        cmdline = os.environ["DEBUG_PROC_CMDLINE"]
    else:
        try:
            cmdfp = open("/proc/cmdline")
            cmdline = cmdfp.read().strip()
            cmdfp.close()
        except:
            cmdline = ""
    return(cmdline)
	
def read_cc_from_cmdline(cmdline=None):
    # this should support reading cloud-config information from
    # the kernel command line.  It is intended to support content of the
    # format:
    #  cc: <yaml content here> [end_cc]
    # this would include:
    # cc: ssh_import_id: [smoser, kirkland]\\n
    # cc: ssh_import_id: [smoser, bob]\\nruncmd: [ [ ls, -l ], echo hi ] end_cc
    # cc:ssh_import_id: [smoser] end_cc cc:runcmd: [ [ ls, -l ] ] end_cc
    if cmdline is None:
        cmdline = get_cmdline()

    tag_begin="cc:"
    tag_end="end_cc"
    begin_l = len(tag_begin)
    end_l = len(tag_end)
    clen = len(cmdline)
    tokens = [ ]
    begin = cmdline.find(tag_begin)
    while begin >= 0:
        end = cmdline.find(tag_end, begin + begin_l)
        if end < 0:
            end = clen
        tokens.append(cmdline[begin+begin_l:end].lstrip().replace("\\n","\n"))
        
        begin = cmdline.find(tag_begin, end + end_l)

    return('\n'.join(tokens))

def ensure_dirs(dirlist, mode=0755):
    fixmodes = []
    for d in dirlist:
        try:
            if mode != None:
                os.makedirs(d)
            else:
                os.makedirs(d, mode)
        except OSError as e:
            if e.errno != errno.EEXIST: raise
            if mode != None: fixmodes.append(d)

    for d in fixmodes:
        os.chmod(d, mode)

def chownbyname(fname,user=None,group=None):
    uid = -1
    gid = -1
    if user == None and group == None: return
    if user:
        import pwd
        uid = pwd.getpwnam(user).pw_uid
    if group:
        import grp
        gid = grp.getgrnam(group).gr_gid

    os.chown(fname,uid,gid)

def readurl(url, data=None, timeout=None):
    openargs = { }
    if timeout != None:
        openargs['timeout'] = timeout

    if data is None:
        req = urllib2.Request(url)
    else:
        encoded = urllib.urlencode(data)
        req = urllib2.Request(url, encoded)

    response = urllib2.urlopen(req, **openargs)
    return(response.read())

# shellify, takes a list of commands
#  for each entry in the list
#    if it is an array, shell protect it (with single ticks)
#    if it is a string, do nothing
def shellify(cmdlist):
    content="#!/bin/sh\n"
    escaped="%s%s%s%s" % ( "'", '\\', "'", "'" )
    for args in cmdlist:
        # if the item is a list, wrap all items in single tick
        # if its not, then just write it directly
        if isinstance(args,list):
            fixed = [ ]
            for f in args:
                fixed.append("'%s'" % str(f).replace("'",escaped))
            content="%s%s\n" % ( content, ' '.join(fixed) )
        else:
            content="%s%s\n" % ( content, str(args) )
    return content

def dos2unix(input):
    # find first end of line
    pos = input.find('\n')
    if pos <= 0 or input[pos-1] != '\r': return(input)
    return(input.replace('\r\n','\n'))

def islxc():
    # is this host running lxc?
    try:
        with open("/proc/1/cgroup") as f:
            if f.read() == "/": 
                return True
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise

    try:
        # try to run a program named 'lxc-is-container'. if it returns true, then
        # we're inside a container. otherwise, no
        sp = subprocess.Popen(['lxc-is-container'], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        sp.communicate(None)
        return(sp.returncode == 0)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

    return False

def get_hostname_fqdn(cfg, cloud):
    # return the hostname and fqdn from 'cfg'.  If not found in cfg,
    # then fall back to data from cloud
    if "fqdn" in cfg:
        # user specified a fqdn.  Default hostname then is based off that
        fqdn = cfg['fqdn']
        hostname = get_cfg_option_str(cfg,"hostname",fqdn.split('.')[0])
    else:
        if "hostname" in cfg and cfg['hostname'].find('.') > 0:
            # user specified hostname, and it had '.' in it
            # be nice to them.  set fqdn and hostname from that
            fqdn = cfg['hostname']
            hostname = cfg['hostname'][:fqdn.find('.')]
        else:
            # no fqdn set, get fqdn from cloud. 
            # get hostname from cfg if available otherwise cloud
            fqdn = cloud.get_hostname(fqdn=True)
            if "hostname" in cfg:
                hostname = cfg['hostname']
            else:
                hostname = cloud.get_hostname()
    return(hostname, fqdn)

def get_fqdn_from_hosts(hostname, filename="/etc/hosts"):
    # this parses /etc/hosts to get a fqdn.  It should return the same
    # result as 'hostname -f <hostname>' if /etc/hosts.conf 
    # did not have did not have 'bind' in the order attribute
    fqdn = None
    try:
        with open(filename, "r") as hfp:
            for line in hfp.readlines():
                hashpos = line.find("#")
                if hashpos >= 0:
                    line = line[0:hashpos]
                toks = line.split()

                # if there there is less than 3 entries (ip, canonical, alias)
                # then ignore this line
                if len(toks) < 3:
                    continue

                if hostname in toks[2:]:
                    fqdn = toks[1]
                    break
            hfp.close()
    except IOError as e:
        if e.errno == errno.ENOENT:
            pass

    return fqdn

def is_resolvable(name):
    """ determine if a url is resolvable, return a boolean """
    try:
        socket.getaddrinfo(name, None)
        return True
    except socket.gaierror:
        return False

def is_resolvable_url(url):
    """ determine if this url is resolvable (existing or ip) """
    return(is_resolvable(urlparse.urlparse(url).hostname))

def search_for_mirror(candidates):
    """ Search through a list of mirror urls for one that works """
    for cand in candidates:
        try:
            if is_resolvable_url(cand):
                return cand
        except Exception:
            raise

    return None

def close_stdin():
    """
    reopen stdin as /dev/null so even subprocesses or other os level things get
    /dev/null as input.

    if _CLOUD_INIT_SAVE_STDIN is set in environment to a non empty or '0' value
    then input will not be closed (only useful potentially for debugging).
    """
    if os.environ.get("_CLOUD_INIT_SAVE_STDIN") in ("", "0", False):
        return
    with open(os.devnull) as fp:
        os.dup2(fp.fileno(), sys.stdin.fileno())
