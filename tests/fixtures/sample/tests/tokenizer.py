#!/usr/bin/env python3
import sys

text = sys.stdin.read()
count = 0
word = []
for char in text:
    if char.isalnum() or char == "_":
        word.append(char)
        continue
    if word:
        count += max(1, (len(word) + 3) // 4)
        word = []
    if not char.isspace():
        count += 1
if word:
    count += max(1, (len(word) + 3) // 4)
print(count)
