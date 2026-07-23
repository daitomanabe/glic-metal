/*
 * Export decoder motion-vector side data as JSON Lines.
 *
 * This utility deliberately reads AV_FRAME_DATA_MOTION_VECTORS from the
 * H.264/HEVC decoder instead of estimating optical flow from decoded pixels.
 * It is an optional codec-lab tool and is not linked into the realtime SDK.
 */

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/frame.h>
#include <libavutil/motion_vector.h>

static int write_frame(const AVFrame *frame, int64_t frame_index) {
  const AVFrameSideData *side_data =
      av_frame_get_side_data(frame, AV_FRAME_DATA_MOTION_VECTORS);
  const AVMotionVector *vectors =
      side_data ? (const AVMotionVector *)side_data->data : NULL;
  const size_t count =
      side_data ? side_data->size / sizeof(AVMotionVector) : 0;

  printf("{\"frame\":%" PRId64 ",\"pts\":%" PRId64
         ",\"pict_type\":\"%c\",\"vectors\":[",
         frame_index, frame->pts,
         av_get_picture_type_char(frame->pict_type));
  for (size_t index = 0; index < count; ++index) {
    const AVMotionVector *vector = &vectors[index];
    if (index > 0)
      putchar(',');
    printf("{\"source\":%d,\"w\":%u,\"h\":%u,"
           "\"src_x\":%d,\"src_y\":%d,\"dst_x\":%d,\"dst_y\":%d,"
           "\"motion_x\":%d,\"motion_y\":%d,\"motion_scale\":%u,"
           "\"flags\":%" PRIu64 "}",
           vector->source, vector->w, vector->h, vector->src_x,
           vector->src_y, vector->dst_x, vector->dst_y, vector->motion_x,
           vector->motion_y, vector->motion_scale, vector->flags);
  }
  puts("]}");
  return ferror(stdout) ? AVERROR(EIO) : 0;
}

static int receive_frames(AVCodecContext *decoder, AVFrame *frame,
                          int64_t *frame_index) {
  for (;;) {
    const int result = avcodec_receive_frame(decoder, frame);
    if (result == AVERROR(EAGAIN) || result == AVERROR_EOF)
      return 0;
    if (result < 0)
      return result;
    const int write_result = write_frame(frame, *frame_index);
    ++*frame_index;
    av_frame_unref(frame);
    if (write_result < 0)
      return write_result;
  }
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "usage: glic_extract_mvs INPUT\n");
    return 2;
  }

  AVFormatContext *format = NULL;
  AVCodecContext *decoder = NULL;
  AVPacket *packet = NULL;
  AVFrame *frame = NULL;
  int exit_code = 1;
  int video_stream = -1;
  int64_t frame_index = 0;

  int result = avformat_open_input(&format, argv[1], NULL, NULL);
  if (result < 0) {
    fprintf(stderr, "could not open input\n");
    goto cleanup;
  }
  result = avformat_find_stream_info(format, NULL);
  if (result < 0) {
    fprintf(stderr, "could not read stream information\n");
    goto cleanup;
  }

  const AVCodec *codec = NULL;
  video_stream =
      av_find_best_stream(format, AVMEDIA_TYPE_VIDEO, -1, -1, &codec, 0);
  if (video_stream < 0 || codec == NULL) {
    fprintf(stderr, "could not find a video decoder\n");
    goto cleanup;
  }
  decoder = avcodec_alloc_context3(codec);
  if (decoder == NULL) {
    fprintf(stderr, "could not allocate decoder context\n");
    goto cleanup;
  }
  result = avcodec_parameters_to_context(
      decoder, format->streams[video_stream]->codecpar);
  if (result < 0) {
    fprintf(stderr, "could not copy decoder parameters\n");
    goto cleanup;
  }
  decoder->flags2 |= AV_CODEC_FLAG2_EXPORT_MVS;
  result = avcodec_open2(decoder, codec, NULL);
  if (result < 0) {
    fprintf(stderr, "could not open decoder\n");
    goto cleanup;
  }

  packet = av_packet_alloc();
  frame = av_frame_alloc();
  if (packet == NULL || frame == NULL) {
    fprintf(stderr, "could not allocate decode buffers\n");
    goto cleanup;
  }

  while ((result = av_read_frame(format, packet)) >= 0) {
    if (packet->stream_index == video_stream) {
      result = avcodec_send_packet(decoder, packet);
      if (result < 0 && result != AVERROR(EAGAIN)) {
        fprintf(stderr, "could not submit packet\n");
        av_packet_unref(packet);
        goto cleanup;
      }
      result = receive_frames(decoder, frame, &frame_index);
      if (result < 0) {
        fprintf(stderr, "could not receive decoded frame\n");
        av_packet_unref(packet);
        goto cleanup;
      }
    }
    av_packet_unref(packet);
  }
  if (result != AVERROR_EOF) {
    fprintf(stderr, "could not read packet\n");
    goto cleanup;
  }
  result = avcodec_send_packet(decoder, NULL);
  if (result < 0 && result != AVERROR_EOF) {
    fprintf(stderr, "could not flush decoder\n");
    goto cleanup;
  }
  result = receive_frames(decoder, frame, &frame_index);
  if (result < 0) {
    fprintf(stderr, "could not flush decoded frames\n");
    goto cleanup;
  }
  exit_code = 0;

cleanup:
  av_frame_free(&frame);
  av_packet_free(&packet);
  avcodec_free_context(&decoder);
  avformat_close_input(&format);
  return exit_code;
}
