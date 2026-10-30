# Changelog

## 0.2.0

- Treat photos: with photos on, each toss returns two photos, 1 and 2 seconds
  after the treat leaves. Video starts just before the toss and stops after.
  A photo problem never stops a treat.
- Each toss reports the moment it left, so double and triple launches are
  timed from the treat rather than from when the photos were ready.
- Adds ffmpeg to the image.

## 0.1.0

- First release: Furbo sign-in with emailed verification code (entered once),
  treat toss and treat sound over the camera's P2P link, a daily limit and a
  minimum interval between treats.
