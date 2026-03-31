createSession() {
  return this.http.post('/api/session', {});
}
