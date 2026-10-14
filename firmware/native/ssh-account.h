#ifndef KF_SSH_ACCOUNT_H
#define KF_SSH_ACCOUNT_H
/* Android has no Unix passwd database. Expose only the root SSH account. */
#include <pwd.h>
#include <grp.h>
#include <string.h>
#include <unistd.h>
static struct passwd *kf_getpwnam(const char *name) {
    static struct passwd account = {
        .pw_name = "root", .pw_passwd = "*", .pw_uid = 0, .pw_gid = 0,
        .pw_gecos = "KaiForward", .pw_dir = "/data/kaiforward/ssh",
        .pw_shell = "/system/bin/sh"
    };
    return strcmp(name, "root") == 0 ? &account : NULL;
}
static struct passwd *kf_getpwuid(uid_t uid) {
    return uid == 0 ? kf_getpwnam("root") : NULL;
}
static int kf_shell_index;
static void kf_setusershell(void) { kf_shell_index = 0; }
static void kf_endusershell(void) { kf_shell_index = 0; }
static char *kf_getusershell(void) {
    return kf_shell_index++ == 0 ? "/system/bin/sh" : NULL;
}
/* Both uid and primary gid remain root. Preserve Android inet membership. */
static int kf_initgroups(const char *name, gid_t gid) {
    return gid == 0 && strcmp(name, "root") == 0 ? 0 : -1;
}
#define initgroups kf_initgroups
#define getpwnam kf_getpwnam
#define getpwuid kf_getpwuid
#define setusershell kf_setusershell
#define endusershell kf_endusershell
#define getusershell kf_getusershell

#endif
