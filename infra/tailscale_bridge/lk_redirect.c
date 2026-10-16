/* LD_PRELOAD shim for the LiveKit agent worker.
 *
 *  - TCP connect() to LK_TARGET_IP:{7880,7881,5678}  -> 127.0.0.1 (served by ts_bridge.py)
 *  - UDP connect()/sendto() to LK_TARGET_IP           -> ENETUNREACH, so WebRTC falls back
 *    to ICE-TCP (7881) immediately instead of waiting for UDP to time out.
 *
 * Build:  gcc -shared -fPIC -O2 lk_redirect.c -o liblk_redirect.so -ldl
 * Run:    LK_TARGET_IP=100.x.y.z LD_PRELOAD=./liblk_redirect.so python3 live_agent.py start
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <errno.h>
#include <string.h>

static int (*real_connect)(int, const struct sockaddr *, socklen_t) = NULL;
static ssize_t (*real_sendto)(int, const void *, size_t, int, const struct sockaddr *, socklen_t) = NULL;
static uint32_t target_ip = 0; /* host byte order; 0 = shim disabled */

__attribute__((constructor))
static void init(void) {
    real_connect = dlsym(RTLD_NEXT, "connect");
    real_sendto = dlsym(RTLD_NEXT, "sendto");
    const char *ip = getenv("LK_TARGET_IP");
    struct in_addr a;
    if (ip && inet_pton(AF_INET, ip, &a) == 1) target_ip = ntohl(a.s_addr);
}

int connect(int sockfd, const struct sockaddr *addr, socklen_t addrlen) {
    if (!real_connect) real_connect = dlsym(RTLD_NEXT, "connect");
    if (target_ip && addr && addr->sa_family == AF_INET && addrlen >= sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
        uint16_t port = ntohs(sin->sin_port);
        if (ntohl(sin->sin_addr.s_addr) == target_ip) {
            int sock_type = 0;
            socklen_t optlen = sizeof(sock_type);
            getsockopt(sockfd, SOL_SOCKET, SO_TYPE, &sock_type, &optlen);
            if (sock_type == SOCK_DGRAM) {
                errno = ENETUNREACH;
                return -1;
            }
            if (port == 7880 || port == 7881 || port == 5678) {
                struct sockaddr_in copy;
                memcpy(&copy, sin, sizeof(copy));
                copy.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
                return real_connect(sockfd, (const struct sockaddr *)&copy, sizeof(copy));
            }
        }
    }
    return real_connect(sockfd, addr, addrlen);
}

ssize_t sendto(int sockfd, const void *buf, size_t len, int flags,
               const struct sockaddr *dest_addr, socklen_t addrlen) {
    if (!real_sendto) real_sendto = dlsym(RTLD_NEXT, "sendto");
    if (target_ip && dest_addr && dest_addr->sa_family == AF_INET && addrlen >= sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)dest_addr;
        if (ntohl(sin->sin_addr.s_addr) == target_ip) {
            errno = ENETUNREACH;
            return -1;
        }
    }
    return real_sendto(sockfd, buf, len, flags, dest_addr, addrlen);
}
