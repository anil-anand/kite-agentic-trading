try {
  JSON.parse('{"value": NaN}');
} catch (e) {
  console.log(e.message);
}
try {
  JSON.parse('{"value": Infinity}');
} catch (e) {
  console.log(e.message);
}
