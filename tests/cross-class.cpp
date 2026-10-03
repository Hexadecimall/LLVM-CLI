struct Pair {
  int left;
  int right;
};

extern "C" int answer() {
  Pair values{20, 22};
  return values.left + values.right;
}
