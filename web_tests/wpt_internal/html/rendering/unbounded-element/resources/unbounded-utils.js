function visual_assert_true(condition, message) {
  if (condition) {
    return;
  }
  const failDiv = document.createElement('div');
  failDiv.style.color = 'red';
  failDiv.style.fontWeight = 'bold';
  failDiv.style.fontSize = '24px';
  failDiv.textContent = `FAIL: ${message || 'Assertion failed'}`;
  document.body.insertBefore(failDiv, document.body.firstChild);
}

async function unbounded_appearance_test(test_function) {
  visual_assert_true(document.documentElement.classList.contains('reftest-wait'),
      "HTML element must have reftest-wait class");
  try {
    await test_function();
  } catch (e) {
    visual_assert_true(false, e);
  } finally {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    document.documentElement.classList.remove('reftest-wait');
  }
}
