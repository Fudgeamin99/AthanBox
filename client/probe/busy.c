// Emulates an HDMI output that takes one playback stream at a time (like hw:CARD,DEV): a second
// snd_pcm_open for playback while one is open fails with EBUSY. Logs every open and close.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <unistd.h>
#include <pthread.h>
#include <stdlib.h>
typedef struct _snd_pcm snd_pcm_t;
static int open_count = 0;
static void *pcms[64];
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static FILE *lf(void){ static FILE *f; if(!f){ const char *p = getenv("BUSY_LOG"); f = p ? fopen(p, "a") : stderr; setvbuf(f, NULL, _IOLBF, 0);} return f; }
int snd_pcm_open(snd_pcm_t **pcm, const char *name, int stream, int mode){
  static int (*real)(snd_pcm_t **, const char *, int, int);
  if(!real) real = dlsym(RTLD_NEXT, "snd_pcm_open");
  pthread_mutex_lock(&mu);
  if(stream == 0 && open_count >= 1 && !getenv("ALSA_SHARED")){
    fprintf(lf(), "[busy] pid %d open '%s' -> EBUSY (already %d open)\n", getpid(), name, open_count);
    pthread_mutex_unlock(&mu); return -EBUSY;
  }
  int r = real(pcm, name, stream, mode);
  if(r == 0 && stream == 0){ open_count++; }
  fprintf(lf(), "[busy] pid %d open '%s' stream %d -> %d (now %d open)\n", getpid(), name, stream, r, open_count);
  pthread_mutex_unlock(&mu); return r;
}
int snd_pcm_close(snd_pcm_t *pcm){
  static int (*real)(snd_pcm_t *);
  if(!real) real = dlsym(RTLD_NEXT, "snd_pcm_close");
  pthread_mutex_lock(&mu);
  open_count--; fprintf(lf(), "[busy] pid %d close (now %d open)\n", getpid(), open_count);
  pthread_mutex_unlock(&mu);
  return real(pcm);
}
