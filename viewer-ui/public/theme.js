(function () {
  var theme = 'system';
  try { var saved = localStorage.getItem('kaiforward.theme'); if (saved === 'light' || saved === 'dark') theme = saved; } catch (error) {}
  var dark = theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.classList.toggle('dark', dark);
}());
