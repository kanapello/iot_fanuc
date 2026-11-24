/*
 * Minimal fake of the Fanuc FOCAS2 library, used only by tests of
 * edge/tools/focas_probe.py. Struct layouts mirror fwlib32.h (0i-D / 30i).
 *
 * Behaviour is selected by the IP address passed to cnc_allclibhndl3:
 *   10.0.0.1 - healthy machine, running, no alarms
 *   10.0.0.2 - machine in alarm, cnc_sysinfo / cnc_exeprgname / cnc_rdspmeter unsupported
 *   anything else - EW_SOCKET
 */
#include <string.h>

#pragma pack(push, 4)
typedef struct { short addinfo; short max_axis; char cnc_type[2]; char mt_type[2]; char series[4]; char version[4]; char axes[2]; } ODBSYS;
typedef struct { short hdck, tmmode, aut, run, motion, mstb, emergency, alarm, edit; } ODBST;
typedef struct { short dummy[2]; short data; short mdata; } ODBPRO;
typedef struct { char name[36]; int o_num; } ODBEXEPRG;
typedef struct { short dummy[2]; int data; } ODBACT;
typedef struct { int data; short dec; short unit; char name; char suff1; char suff2; char reserve; } LOADELM;
typedef struct { LOADELM spload; LOADELM spspeed; } ODBSPLOAD;
typedef struct { short datano; short dummy; int mcr_val; short dec_val; } ODBM;
typedef struct { short datano; short type; int ldata; } IODBPSD;
typedef struct { int alm_no; short type; short axis; short dummy; short msg_len; char alm_msg[64]; } ODBALMMSG2;
#pragma pack(pop)

#define EW_OK 0
#define EW_FUNC 1
#define EW_NUMBER 3
#define EW_HANDLE (-8)
#define EW_SOCKET (-16)

static int mode = 0;

short cnc_startupprocess(int level, const char *filename) { (void)level; (void)filename; return EW_OK; }
short cnc_exitprocess(void) { return EW_OK; }

short cnc_allclibhndl3(const char *ip, unsigned short port, int timeout, unsigned short *h) {
    (void)timeout;
    if (port != 8193) return EW_SOCKET;
    if (strcmp(ip, "10.0.0.1") == 0) mode = 1;
    else if (strcmp(ip, "10.0.0.2") == 0) mode = 2;
    else return EW_SOCKET;
    *h = 7;
    return EW_OK;
}

short cnc_freelibhndl(unsigned short h) { (void)h; mode = 0; return EW_OK; }

#define CHECK_HANDLE if (h != 7 || mode == 0) return EW_HANDLE

short cnc_sysinfo(unsigned short h, ODBSYS *s) {
    CHECK_HANDLE;
    if (mode == 2) return EW_FUNC;
    memset(s, 0, sizeof(*s));
    s->addinfo = 0x0002;  /* i series */
    s->max_axis = 32;
    memcpy(s->cnc_type, " 0", 2); memcpy(s->mt_type, " M", 2);
    memcpy(s->series, "D4F1", 4); memcpy(s->version, "16.0", 4); memcpy(s->axes, "03", 2);
    return EW_OK;
}

short cnc_statinfo(unsigned short h, ODBST *s) {
    CHECK_HANDLE;
    memset(s, 0, sizeof(*s));
    s->tmmode = 1; s->aut = 1;
    if (mode == 1) { s->run = 3; s->motion = 1; }
    else { s->run = 1; s->alarm = 1; }
    return EW_OK;
}

short cnc_rdprgnum(unsigned short h, ODBPRO *p) {
    CHECK_HANDLE;
    p->data = 1234; p->mdata = 1000;
    return EW_OK;
}

short cnc_exeprgname(unsigned short h, ODBEXEPRG *p) {
    CHECK_HANDLE;
    if (mode == 2) return EW_FUNC;
    memset(p, 0, sizeof(*p));
    strcpy(p->name, "O1234");
    p->o_num = 1234;
    return EW_OK;
}

short cnc_actf(unsigned short h, ODBACT *a) { CHECK_HANDLE; a->data = (mode == 1) ? 1250 : 0; return EW_OK; }
short cnc_acts(unsigned short h, ODBACT *a) { CHECK_HANDLE; a->data = (mode == 1) ? 8000 : 0; return EW_OK; }

short cnc_rdspmeter(unsigned short h, short type, short *num, ODBSPLOAD *l) {
    CHECK_HANDLE;
    (void)type;
    if (mode == 2) return EW_FUNC;
    if (*num < 1) return EW_NUMBER;
    memset(l, 0, sizeof(*l));
    l[0].spload.data = 375; l[0].spload.dec = 1; l[0].spload.name = 'S'; l[0].spload.suff1 = '1';
    *num = 1;
    return EW_OK;
}

short cnc_rdmacro(unsigned short h, short number, short length, ODBM *m) {
    CHECK_HANDLE;
    (void)length;
    if (number != 3901) return EW_NUMBER;
    m->datano = number; m->mcr_val = 15873000; m->dec_val = 3;
    return EW_OK;
}

short cnc_rdparam(unsigned short h, short number, short axis, short length, IODBPSD *p) {
    CHECK_HANDLE;
    (void)axis; (void)length;
    if (number != 6711) return EW_NUMBER;
    p->datano = number; p->ldata = 15873;
    return EW_OK;
}

short cnc_rdalmmsg2(unsigned short h, short type, short *num, ODBALMMSG2 *a) {
    CHECK_HANDLE;
    (void)type;
    if (mode != 2) { *num = 0; return EW_OK; }
    memset(a, 0, sizeof(*a));
    a[0].alm_no = 401; a[0].type = 6; a[0].axis = 1;
    strcpy(a[0].alm_msg, "V-READY OFF");
    a[0].msg_len = (short)strlen(a[0].alm_msg);
    *num = 1;
    return EW_OK;
}
