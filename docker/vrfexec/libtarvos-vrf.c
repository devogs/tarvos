// SPDX-License-Identifier: Apache-2.0
//
// libtarvos-vrf.so: LD_PRELOAD library used by vrfexec(1). Every IPv4/IPv6 socket the
// program creates is bound to the VRF device named by TARVOS_VRF (SO_BINDTODEVICE), so
// its traffic uses that VRF's routing table.
//
// Why not "ip vrf exec": it needs a writable cgroup v2 hierarchy, which a non-privileged
// container does not have, and root. SO_BINDTODEVICE on a socket that is not bound yet
// needs no privilege since Linux 5.7, so this works for admin and TACACS+ users too.
//
// Fails closed: if the socket cannot be bound, it is closed and socket() fails, so the
// program never silently talks from the wrong VRF.
//
// Installed with the set-user-ID bit in a system library directory: glibc only preloads
// such libraries into programs that run in secure-execution mode (file capabilities:
// ping, arping, mtr-packet, tcpdump). It grants nothing; it only restricts sockets.

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <net/if.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int (*real_socket)(int, int, int);

int socket(int domain, int type, int protocol)
{
	if (!real_socket) {
		real_socket = (int (*)(int, int, int))dlsym(RTLD_NEXT, "socket");
		if (!real_socket) {
			errno = ENOSYS;
			return -1;
		}
	}
	int fd = real_socket(domain, type, protocol);
	if (fd < 0 || (domain != AF_INET && domain != AF_INET6))
		return fd;

	const char *vrf = getenv("TARVOS_VRF");
	if (!vrf || !*vrf)
		return fd;
	size_t len = strnlen(vrf, IF_NAMESIZE);
	int err = EINVAL;
	if (len < IF_NAMESIZE &&
	    setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, vrf, (socklen_t)len) == 0)
		return fd;
	if (len < IF_NAMESIZE)
		err = errno;
	close(fd);
	fprintf(stderr, "vrfexec: cannot bind socket to VRF %.*s: %s\n", (int)len, vrf,
		strerror(err));
	errno = err;
	return -1;
}
