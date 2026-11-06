// 静音音频保活：AVAudioEngine 输出全零采样，让 iOS 认为 App 在播放音频，
// 从而不在后台挂起。无需音频文件，CPU 占用极低。
#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <UIKit/UIKit.h>

static AVAudioEngine *gEngine = nil;
static AVAudioSourceNode *gSrc = nil;

static void kgStart(void) {
    if (gEngine && gEngine.isRunning) return;
    NSError *err = nil;
    AVAudioSession *s = [AVAudioSession sharedInstance];
    [s setCategory:AVAudioSessionCategoryPlayback
       withOptions:AVAudioSessionCategoryOptionMixWithOthers
             error:&err];
    [s setActive:YES error:&err];

    if (!gEngine) {
        gEngine = [[AVAudioEngine alloc] init];
        AVAudioFormat *fmt = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:44100
                                                                           channels:1];
        gSrc = [[AVAudioSourceNode alloc] initWithFormat:fmt
            renderBlock:^OSStatus(BOOL *isSilence, const AudioTimeStamp *ts,
                                  AVAudioFrameCount frames, AudioBufferList *buf) {
                for (UInt32 i = 0; i < buf->mNumberBuffers; i++)
                    memset(buf->mBuffers[i].mData, 0, buf->mBuffers[i].mDataByteSize);
                if (isSilence) *isSilence = YES;
                return noErr;
            }];
        [gEngine attachNode:gSrc];
        [gEngine connect:gSrc to:gEngine.mainMixerNode format:fmt];
        [gEngine prepare];
    }
    [gEngine startAndReturnError:&err];
    NSLog(@"[keepalive] audio engine running=%d err=%@", gEngine.isRunning, err);
}

__attribute__((constructor))
static void kgInit(void) {
    dispatch_async(dispatch_get_main_queue(), ^{
        kgStart();

        [[NSNotificationCenter defaultCenter]
            addObserverForName:AVAudioSessionInterruptionNotification
                        object:nil queue:[NSOperationQueue mainQueue]
                    usingBlock:^(NSNotification *n) {
            NSInteger t = [n.userInfo[AVAudioSessionInterruptionTypeKey] integerValue];
            if (t == AVAudioSessionInterruptionTypeEnded) {
                kgStart();
            } else {
                dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1 * NSEC_PER_SEC)),
                               dispatch_get_main_queue(), ^{ kgStart(); });
            }
        }];

        [[NSNotificationCenter defaultCenter]
            addObserverForName:UIApplicationDidBecomeActiveNotification
                        object:nil queue:[NSOperationQueue mainQueue]
                    usingBlock:^(NSNotification *n) { kgStart(); }];

        [NSTimer scheduledTimerWithTimeInterval:30 repeats:YES
                                          block:^(NSTimer *t) { kgStart(); }];
    });
}
