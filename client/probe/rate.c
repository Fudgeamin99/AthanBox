// Logs the sound format Chromium asks ALSA for on each playback stream: the device, sample rate,
// channels and latency, and the rate the hardware parameters end up at.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
static FILE *lf(void){ static FILE *f; if(!f){ const char *p = getenv("RATE_LOG"); f = p ? fopen(p, "a") : stderr; setvbuf(f, NULL, _IOLBF, 0);} return f; }
static double now(void){ struct timespec t; clock_gettime(CLOCK_REALTIME, &t); return t.tv_sec + t.tv_nsec / 1e9; }
int snd_pcm_open(snd_pcm_t **pcm, const char *name, int stream, int mode){
  static int (*real)(snd_pcm_t **, const char *, int, int);
  if(!real) real = dlsym(RTLD_NEXT, "snd_pcm_open");
  int r = real(pcm, name, stream, mode);
  if(stream == 0) fprintf(lf(), "%.1f [rate] pid %d open '%s' -> %d pcm %p\n", now(), getpid(), name, r, r == 0 ? (void *)*pcm : NULL);
  return r;
}
int snd_pcm_set_params(snd_pcm_t *pcm, int format, int access, unsigned channels, unsigned rate, int resample, unsigned latency){
  static int (*real)(snd_pcm_t *, int, int, unsigned, unsigned, int, unsigned);
  if(!real) real = dlsym(RTLD_NEXT, "snd_pcm_set_params");
  int r = real(pcm, format, access, channels, rate, resample, latency);
  fprintf(lf(), "%.1f [rate] pid %d set_params pcm %p format %d channels %u rate %u resample %d latency %u us -> %d\n",
          now(), getpid(), (void *)pcm, format, channels, rate, resample, latency, r);
  return r;
}
int snd_pcm_hw_params(snd_pcm_t *pcm, snd_pcm_hw_params_t *params){
  static int (*real)(snd_pcm_t *, snd_pcm_hw_params_t *);
  static int (*get_rate)(const snd_pcm_hw_params_t *, unsigned *, int *);
  if(!real) real = dlsym(RTLD_NEXT, "snd_pcm_hw_params");
  if(!get_rate) get_rate = dlsym(RTLD_NEXT, "snd_pcm_hw_params_get_rate");
  int r = real(pcm, params);
  unsigned rate = 0; int dir = 0;
  if(r == 0 && get_rate) get_rate(params, &rate, &dir);
  fprintf(lf(), "%.1f [rate] pid %d hw_params pcm %p rate %u -> %d\n", now(), getpid(), (void *)pcm, rate, r);
  return r;
}
int snd_pcm_close(snd_pcm_t *pcm){
  static int (*real)(snd_pcm_t *);
  if(!real) real = dlsym(RTLD_NEXT, "snd_pcm_close");
  fprintf(lf(), "%.1f [rate] pid %d close pcm %p\n", now(), getpid(), (void *)pcm);
  return real(pcm);
}
