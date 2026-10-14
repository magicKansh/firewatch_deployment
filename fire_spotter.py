from PIL import Image
import numpy as np

image_path = "images/thermal.jpg"

rows = 8
cols = 8

image = Image.open(image_path).convert("RGB")
image_array = np.array(image)

height, width, _ = image_array.shape

chunk_height = height
chunk_width = width

print(f"image size: {width} x {height}")
print(f"chunk size: {chunk_width} x {chunk_height}")
print()

for row in range(rows):
    for col in range(cols):

        y1 = row * chunk_height
        y2 = (row + 1) * chunk_height

        x1 = col * chunk_width
        x2 = (col + 1) * chunk_width

        chunk = image_array[y1:y2, x1:x2]

        average_color = chunk.mean(axis=(0, 1))

        r, g, b = average_color

        print(
            f"[{row+1},{col+1}] "
            f"RGB=({r:.0f}, {g:.0f}, {b:.0f})"
        )