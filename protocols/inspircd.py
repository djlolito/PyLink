import time
import sys
import os
import traceback
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
from copy import copy
from log import log

from classes import *

# Raw commands sent from servers vary from protocol to protocol. Here, we map
# non-standard names to our hook handlers, so plugins get the information they need.
hook_map = {'FJOIN': 'JOIN', 'SAVE': 'NICK',
            'RSQUIT': 'SQUIT', 'FMODE': 'MODE'}

def _sendFromServer(irc, sid, msg):
    irc.send(':%s %s' % (sid, msg))

def _sendFromUser(irc, numeric, msg):
    irc.send(':%s %s' % (numeric, msg))

def spawnClient(irc, nick, ident, host, modes=[], server=None, *args):
    server = server or irc.sid
    if not utils.isInternalServer(irc, server):
        raise ValueError('Server %r is not a PyLink internal PseudoServer!' % server)
    # We need a separate UID generator instance for every PseudoServer
    # we spawn. Otherwise, things won't wrap around properly.
    if server not in irc.uidgen:
        irc.uidgen[server] = utils.TS6UIDGenerator(server)
    uid = irc.uidgen[server].next_uid()
    ts = int(time.time())
    if modes:
        modes = utils.joinModes(modes)
    else:
        modes = '+'
    if not utils.isNick(nick):
        raise ValueError('Invalid nickname %r.' % nick)
    _sendFromServer(irc, server, "UID {uid} {ts} {nick} {host} {host} {ident} 0.0.0.0 "
                    "{ts} {modes} + :PyLink Client".format(ts=ts, host=host,
                                             nick=nick, ident=ident, uid=uid,
                                             modes=modes))
    u = irc.users[uid] = IrcUser(nick, ts, uid, ident, host, *args)
    irc.servers[server].users.append(uid)
    return u

def joinClient(irc, client, channel):
    channel = channel.lower()
    server = utils.isInternalClient(irc, client)
    if not server:
        raise LookupError('No such PyLink PseudoClient exists.')
    if not utils.isChannel(channel):
        raise ValueError('Invalid channel name %r.' % channel)
    # One channel per line here!
    _sendFromServer(irc, server, "FJOIN {channel} {ts} + :,{uid}".format(
            ts=int(time.time()), uid=client, channel=channel))
    irc.channels[channel].users.add(client)

def partClient(irc, client, channel, reason=None):
    channel = channel.lower()
    if not utils.isInternalClient(irc, client):
        raise LookupError('No such PyLink PseudoClient exists.')
    msg = "PART %s" % channel
    if not utils.isChannel(channel):
        raise ValueError('Invalid channel name %r.' % channel)
    if reason:
        msg += " :%s" % reason
    _sendFromUser(irc, client, msg)
    handle_part(irc, client, 'PART', [channel])

def removeClient(irc, numeric):
    """<irc object> <client numeric>

    Removes a client from our internal databases, regardless
    of whether it's one of our pseudoclients or not."""
    for v in irc.channels.values():
        v.removeuser(numeric)
    sid = numeric[:3]
    log.debug('Removing client %s from irc.users', numeric)
    del irc.users[numeric]
    log.debug('Removing client %s from irc.servers[%s]', numeric, sid)
    irc.servers[sid].users.remove(numeric)

def quitClient(irc, numeric, reason):
    """<irc object> <client numeric>

    Quits a PyLink PseudoClient."""
    if utils.isInternalClient(irc, numeric):
        _sendFromUser(irc, numeric, "QUIT :%s" % reason)
        removeClient(irc, numeric)
    else:
        raise LookupError("No such PyLink PseudoClient exists. If you're trying to remove "
                          "a user that's not a PyLink PseudoClient from "
                          "the internal state, use removeClient() instead.")

def kickClient(irc, numeric, channel, target, reason=None):
    """<irc object> <kicker client numeric>

    Sends a kick from a PyLink PseudoClient."""
    channel = channel.lower()
    if not utils.isInternalClient(irc, numeric):
        raise LookupError('No such PyLink PseudoClient exists.')
    if not reason:
        reason = 'No reason given'
    _sendFromUser(irc, numeric, 'KICK %s %s :%s' % (channel, target, reason))
    # We can pretend the target left by its own will; all we really care about
    # is that the target gets removed from the channel userlist, and calling
    # handle_part() does that just fine.
    handle_part(irc, target, 'KICK', [channel])

def nickClient(irc, numeric, newnick):
    """<irc object> <client numeric> <new nickname>

    Changes the nick of a PyLink PseudoClient."""
    if not utils.isInternalClient(irc, numeric):
        raise LookupError('No such PyLink PseudoClient exists.')
    if not utils.isNick(newnick):
        raise ValueError('Invalid nickname %r.' % nick)
    _sendFromUser(irc, numeric, 'NICK %s %s' % (newnick, int(time.time())))
    irc.users[numeric].nick = newnick

def connect(irc):
    irc.start_ts = ts = int(time.time())
    irc.uidgen = {}
    host = irc.serverdata["hostname"]
    irc.servers[irc.sid] = IrcServer(None, host, internal=True)

    f = irc.send
    f('CAPAB START 1202')
    f('CAPAB CAPABILITIES :PROTOCOL=1202')
    f('CAPAB END')
    f('SERVER {host} {Pass} 0 {sid} :PyLink Service'.format(host=host,
      Pass=irc.serverdata["sendpass"], sid=irc.sid))
    f(':%s BURST %s' % (irc.sid, ts))
    irc.pseudoclient = spawnClient(irc, 'PyLink', 'pylink', host, modes=set([("+o", None)]))
    f(':%s ENDBURST' % (irc.sid))
    for chan in irc.serverdata['channels']:
        joinClient(irc, irc.pseudoclient.uid, chan)

def handle_ping(irc, source, command, args):
    # <- :70M PING 70M 0AL
    # -> :0AL PONG 0AL 70M
    if utils.isInternalServer(irc, args[1]):
        _sendFromServer(irc, args[1], 'PONG %s %s' % (args[1], source))

def handle_privmsg(irc, source, command, args):
    prefix = irc.conf['bot']['prefix']
    if args[0] == irc.pseudoclient.uid:
        cmd_args = args[1].split(' ')
        cmd = cmd_args[0].lower()
        try:
            cmd_args = cmd_args[1:]
        except IndexError:
            cmd_args = []
        try:
            func = utils.bot_commands[cmd]
        except KeyError:
            utils.msg(irc, source, 'Unknown command %r.' % cmd)
            return
        try:
            func(irc, source, cmd_args)
        except Exception as e:
            traceback.print_exc()
            utils.msg(irc, source, 'Uncaught exception in command %r: %s: %s' % (cmd, type(e).__name__, str(e)))
            return
    return {'target': args[0], 'text': args[1]}

def handle_kill(irc, source, command, args):
    killed = args[0]
    removeClient(irc, killed)
    if killed == irc.pseudoclient.uid:
        irc.pseudoclient = spawnClient(irc, 'PyLink', 'pylink', irc.serverdata["hostname"])
        for chan in irc.serverdata['channels']:
            joinClient(irc, irc.pseudoclient.uid, chan)
    return {'target': killed, 'reason': args[1]}

def handle_kick(irc, source, command, args):
    # :70MAAAAAA KICK #endlessvoid 70MAAAAAA :some reason
    channel = args[0].lower()
    kicked = args[1]
    handle_part(irc, kicked, 'KICK', [channel, args[2]])
    if kicked == irc.pseudoclient.uid:
        joinClient(irc, irc.pseudoclient.uid, channel)
    return {'channel': channel, 'target': kicked, 'reason': args[2]}

def handle_part(irc, source, command, args):
    channel = args[0].lower()
    # We should only get PART commands for channels that exist, right??
    irc.channels[channel].removeuser(source)
    try:
        reason = args[1]
    except IndexError:
        reason = ''
    return {'channel': channel, 'reason': reason}

def handle_error(irc, numeric, command, args):
    irc.connected = False
    raise ProtocolError('Received an ERROR, killing!')

def handle_fjoin(irc, servernumeric, command, args):
    # :70M FJOIN #chat 1423790411 +AFPfjnt 6:5 7:5 9:5 :o,1SRAABIT4 v,1IOAAF53R <...>
    channel = args[0].lower()
    # InspIRCd sends each user's channel data in the form of 'modeprefix(es),UID'
    userlist = args[-1].split()
    ts = args[1]
    modestring = args[2:-1] or args[2]
    utils.applyModes(irc, channel, utils.parseModes(irc, channel, modestring))
    namelist = []
    for user in userlist:
        modeprefix, user = user.split(',', 1)
        namelist.append(user)
        utils.applyModes(irc, channel, [('+%s' % mode, user) for mode in modeprefix])
        irc.channels[channel].users.add(user)
    return {'channel': channel, 'users': namelist}

def handle_uid(irc, numeric, command, args):
    # :70M UID 70MAAAAAB 1429934638 GL 0::1 hidden-7j810p.9mdf.lrek.0000.0000.IP gl 0::1 1429934638 +Wioswx +ACGKNOQXacfgklnoqvx :realname
    uid, ts, nick, realhost, host, ident, ip = args[0:7]
    realname = args[-1]
    irc.users[uid] = IrcUser(nick, ts, uid, ident, host, realname, realhost, ip)
    parsedmodes = utils.parseModes(irc, uid, [args[8], args[9]])
    log.debug('Applying modes %s for %s', parsedmodes, uid)
    utils.applyModes(irc, uid, parsedmodes)
    irc.servers[numeric].users.append(uid)
    return {'uid': uid, 'ts': ts, 'nick': nick, 'realhost': realhost, 'host': host, 'ident': ident, 'ip': ip}

def handle_quit(irc, numeric, command, args):
    # <- :1SRAAGB4T QUIT :Quit: quit message goes here
    removeClient(irc, numeric)
    return {'reason': args[0]}

def handle_burst(irc, numeric, command, args):
    # BURST is sent by our uplink when we link.
    # <- :70M BURST 1433044587

    # This is handled in handle_events, since our uplink
    # only sends its name in the initial authentication phase,
    # not in any following BURST commands.
    pass

def handle_server(irc, numeric, command, args):
    # SERVER is sent by our uplink or any other server to introduce others.
    # <- :00A SERVER test.server * 1 00C :testing raw message syntax
    # <- :70M SERVER millennium.overdrive.pw * 1 1ML :a relatively long period of time... (Fremont, California)
    servername = args[0].lower()
    sid = args[3]
    irc.servers[sid] = IrcServer(numeric, servername)

def handle_nick(irc, numeric, command, args):
    # <- :70MAAAAAA NICK GL-devel 1434744242
    n = irc.users[numeric].nick = args[0]
    return {'target': n, 'ts': args[1]}

def handle_save(irc, numeric, command, args):
    # This is used to handle nick collisions. Here, the client Derp_ already exists,
    # so trying to change nick to it will cause a nick collision. On InspIRCd,
    # this will simply set the collided user's nick to its UID.

    # <- :70MAAAAAA PRIVMSG 0AL000001 :nickclient PyLink Derp_
    # -> :0AL000001 NICK Derp_ 1433728673
    # <- :70M SAVE 0AL000001 1433728673
    user = args[0]
    irc.users[user].nick = user
    return {'target': user, 'ts': args[1]}

def handle_fmode(irc, numeric, command, args):
    # <- :70MAAAAAA FMODE #chat 1433653462 +hhT 70MAAAAAA 70MAAAAAD
    channel = args[0].lower()
    modes = args[2:]
    changedmodes = utils.parseModes(irc, channel, modes)
    utils.applyModes(irc, channel, changedmodes)
    return {'target': channel, 'modes': changedmodes}

def handle_mode(irc, numeric, command, args):
    # In InspIRCd, MODE is used for setting user modes and
    # FMODE is used for channel modes:
    # <- :70MAAAAAA MODE 70MAAAAAA -i+xc
    target = args[0]
    modestrings = args[1:]
    changedmodes = utils.parseModes(irc, numeric, modestrings)
    utils.applyModes(irc, numeric, changedmodes)
    return {'target': target, 'modes': changedmodes}

def handle_squit(irc, numeric, command, args):
    # :70M SQUIT 1ML :Server quit by GL!gl@0::1
    split_server = args[0]
    log.info('(%s) Netsplit on server %s', irc.name, split_server)
    # Prevent RuntimeError: dictionary changed size during iteration
    old_servers = copy(irc.servers)
    for sid, data in old_servers.items():
        if data.uplink == split_server:
            log.debug('Server %s also hosts server %s, removing those users too...', split_server, sid)
            handle_squit(irc, sid, 'SQUIT', [sid, "PyLink: Automatically splitting leaf servers of %s" % sid])
    for user in copy(irc.servers[split_server].users):
        log.debug('Removing client %s (%s)', user, irc.users[user].nick)
        removeClient(irc, user)
    del irc.servers[split_server]
    return {'target': split_server}

def handle_rsquit(irc, numeric, command, args):
    # <- :1MLAAAAIG RSQUIT :ayy.lmao
    # <- :1MLAAAAIG RSQUIT ayy.lmao :some reason
    # RSQUIT is sent by opers to squit remote servers.
    # Strangely, it takes a server name instead of a SID, and is
    # allowed to be ignored entirely.
    # If we receive a remote SQUIT, split the target server
    # ONLY if the sender is identified with us.
    target = args[0]
    for (sid, server) in irc.servers.items():
        if server.name == target:
            target = sid
    if utils.isInternalServer(irc, target):
        if irc.users[numeric].identified:
            uplink = irc.servers[target].uplink
            reason = 'Requested by %s' % irc.users[numeric].nick
            _sendFromServer(irc, uplink, 'SQUIT %s :%s' % (target, reason))
            return handle_squit(irc, numeric, 'SQUIT', [target, reason])
        else:
            utils.msg(irc, numeric, 'Error: you are not authorized to split servers!', notice=True)

def handle_idle(irc, numeric, command, args):
    """Handle the IDLE command, sent between servers in remote WHOIS queries."""
    # <- :70MAAAAAA IDLE 1MLAAAAIG
    # -> :1MLAAAAIG IDLE 70MAAAAAA 1433036797 319
    sourceuser = numeric
    targetuser = args[0]
    _sendFromUser(irc, targetuser, 'IDLE %s %s 0' % (sourceuser, irc.users[targetuser].ts))

def handle_events(irc, data):
    # Each server message looks something like this:
    # :70M FJOIN #chat 1423790411 +AFPfjnt 6:5 7:5 9:5 :v,1SRAAESWE
    # :<sid> <command> <argument1> <argument2> ... :final multi word argument
    args = data.split()
    if not args:
        # No data??
        return
    if args[0] == 'SERVER':
       # SERVER whatever.net abcdefgh 0 10X :something
       servername = args[1].lower()
       numeric = args[4]
       if args[2] != irc.serverdata['recvpass']:
            # Check if recvpass is correct
            raise ProtocolError('Error: recvpass from uplink server %s does not match configuration!' % servername)
       irc.servers[numeric] = IrcServer(None, servername)
       return
    elif args[0] == 'CAPAB':
        # Capability negotiation with our uplink
        if args[1] == 'CHANMODES':
            # CAPAB CHANMODES :admin=&a allowinvite=A autoop=w ban=b banexception=e blockcolor=c c_registered=r exemptchanops=X filter=g flood=f halfop=%h history=H invex=I inviteonly=i joinflood=j key=k kicknorejoin=J limit=l moderated=m nickflood=F noctcp=C noextmsg=n nokick=Q noknock=K nonick=N nonotice=T official-join=!Y op=@o operonly=O opmoderated=U owner=~q permanent=P private=p redirect=L reginvite=R regmoderated=M secret=s sslonly=z stripcolor=S topiclock=t voice=+v

            # Named modes are essential for a cross-protocol IRC service. We
            # can use InspIRCd as a model here and assign their mode map to our cmodes list.
            for modepair in args[2:]:
                name, char = modepair.split('=')
                # We don't really care about mode prefixes; just the mode char
                irc.cmodes[name.lstrip(':')] = char[-1]
        elif args[1] == 'USERMODES':
            # Ditto above.
            for modepair in args[2:]:
                name, char = modepair.split('=')
                irc.umodes[name.lstrip(':')] = char
        elif args[1] == 'CAPABILITIES':
            caps = dict([x.lstrip(':').split('=') for x in args[2:]])
            irc.maxnicklen = caps['NICKMAX']
            irc.maxchanlen = caps['CHANMAX']
            # Modes are divided into A, B, C, and D classes
            # See http://www.irc.org/tech_docs/005.html
            # FIXME: Find a better way to assign/store this.
            irc.cmodes['*A'], irc.cmodes['*B'], irc.cmodes['*C'], irc.cmodes['*D'] \
                = caps['CHANMODES'].split(',')
            irc.umodes['*A'], irc.umodes['*B'], irc.umodes['*C'], irc.umodes['*D'] \
                = caps['USERMODES'].split(',')
            irc.prefixmodes = re.search(r'\((.*?)\)', caps['PREFIX']).group(1)
    try:
        real_args = []
        for arg in args:
            real_args.append(arg)
            # If the argument starts with ':' and ISN'T the first argument.
            # The first argument is used for denoting the source UID/SID.
            if arg.startswith(':') and args.index(arg) != 0:
                # : is used for multi-word arguments that last until the end
                # of the message. We can use list splicing here to turn them all
                # into one argument.
                index = args.index(arg)  # Get the array index of the multi-word arg
                # Set the last arg to a joined version of the remaining args
                arg = args[index:]
                arg = ' '.join(arg)[1:]
                # Cut the original argument list right before the multi-word arg,
                # and then append the multi-word arg.
                real_args = args[:index]
                real_args.append(arg)
                break
        real_args[0] = real_args[0].split(':', 1)[1]
        args = real_args

        numeric = args[0]
        command = args[1]
        args = args[2:]
    except IndexError:
        return

    # We will do wildcard event handling here. Unhandled events are just ignored.
    try:
        func = globals()['handle_'+command.lower()]
    except KeyError:  # unhandled event
        pass
    else:
        parsed_args = func(irc, numeric, command, args)
        # Only call our hooks if there's data to process. Handlers that support
        # hooks will return a dict of parsed arguments, which can be passed on
        # to plugins and the like. For example, the JOIN handler will return
        # something like: {'channel': '#whatever', 'users': ['UID1', 'UID2',
        # 'UID3']}, etc.
        if parsed_args:
            hook_cmd = command
            if command in hook_map:
                hook_cmd = hook_map[command]
            print('Parsed args %r received from %s handler (calling hook %s)' % (parsed_args, command, hook_cmd))
            # Iterate over hooked functions, catching errors accordingly
            for hook_func in utils.command_hooks[hook_cmd]:
                try:
                    print('Calling function %s' % hook_func)
                    hook_func(irc, numeric, command, parsed_args)
                except Exception:
                    # We don't want plugins to crash our servers...
                    traceback.print_exc()
                    continue

def spawnServer(irc, name, sid, uplink=None, desc='PyLink Server'):
    # -> :0AL SERVER test.server * 1 0AM :some silly pseudoserver
    uplink = uplink or irc.sid
    name = name.lower()
    assert len(sid) == 3, "Incorrect SID length"
    if sid in irc.servers:
        raise ValueError('A server with SID %r already exists!' % sid)
    for server in irc.servers.values():
        if name == server.name:
            raise ValueError('A server named %r already exists!' % name)
    if not utils.isInternalServer(irc, uplink):
        raise ValueError('Server %r is not a PyLink internal PseudoServer!' % uplink)
    if not utils.isServerName(name):
        raise ValueError('Invalid server name %r' % name)
    _sendFromServer(irc, uplink, 'SERVER %s * 1 %s :%s' % (name, sid, desc))
    _sendFromServer(irc, sid, 'ENDBURST')
    irc.servers[sid] = IrcServer(uplink, name, internal=True)
