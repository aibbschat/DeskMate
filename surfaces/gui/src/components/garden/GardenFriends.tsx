/**
 * GardenFriends — 常驻的朋友（蓝图第二层 · 常驻的朋友·你的 Agent 各有小窝）。
 *
 * 一直陪着你的小桔/知了在这里有自己的小窝；勋章也收在这里（从勋章 Tab 迁来）。
 */

import { BadgeWall } from "../deskmate/BadgeWall";
import { XiaojuMascot } from "../deskmate/XiaojuMascot";

export function GardenFriends({ onBackHome }: { onBackHome: () => void }) {
  return (
    <div className="garden-friends">
      <div className="garden-space-head">
        <div className="garden-space-title">🫧 常驻的朋友</div>
        <div className="garden-space-sub">一直陪你的小桔，在这座花园里有个小窝；你攒下的勋章也收在这里。</div>
      </div>

      <div className="garden-friend-hut">
        <div className="garden-friend-mascot">
          <div className="garden-friend-halo" />
          <XiaojuMascot scale={0.44} emotion="idle" />
        </div>
        <div className="garden-friend-info">
          <div className="garden-friend-name">小桔</div>
          <div className="garden-friend-role">你的私人 Agent · 园丁</div>
          <div className="garden-friend-desc">
            住在花园里：每天清早来看你，傍晚记得今天的事，深夜给花园留一盏灯。
            你写的字、做成的选择，它都记进花里；它偶尔也给你留信、在日记下回响。
          </div>
        </div>
      </div>

      <div className="garden-space-subhead">勋章墙</div>
      <BadgeWall onBack={onBackHome} />
    </div>
  );
}
