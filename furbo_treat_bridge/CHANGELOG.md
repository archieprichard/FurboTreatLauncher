# Changelog

## 0.3.0

- Photos can be taken at any of up to 5 moments after a toss (the
  integration offers 1, 2 and 3 seconds). Video runs only as long as the
  latest one needs.
- Photo capture fixes: never built on the camera's small preview frame,
  never from frames left over from the previous toss, and decodable when a
  keyframe lacks its SPS/PPS headers. A photo problem can't fail a toss.

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
