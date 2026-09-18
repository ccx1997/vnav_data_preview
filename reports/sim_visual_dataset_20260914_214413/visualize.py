"""Create review figures from existing JPEGs and saved geometry, without re-rendering."""
from __future__ import annotations

import collections
import io
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from analyze import OUT, ROOT, VERSIONS, CAMERAS

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
COLORS = ['#2563eb', '#dc2626', '#7c3aed']


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


def choose_cases(rows):
    valid = [r for r in rows if not r['issues']]
    chosen = []
    def pick(version, mode, field, quantile, title, title_zh, predicate=lambda r: True):
        pool = [r for r in valid if r['generation_settings_version'] == version and r['mode'] == mode and predicate(r) and r['sample_id'] not in {c['sample_id'] for c in chosen}]
        target = np.quantile([r[field] for r in pool], quantile)
        row = min(pool, key=lambda r:(abs(r[field]-target), r['sample_id']))
        chosen.append(dict(row, case_number=len(chosen)+1, title=title, title_zh=title_zh, selection={'field':field,'quantile':quantile,'target':float(target)}))
    new, old = VERSIONS[1], VERSIONS[0]
    pick(new,'indoor','route_length_m',.5,'New / indoor / typical length','新版室内：典型路线长度')
    pick(new,'indoor','obstacle_count',.95,'New / indoor / many obstacles','新版室内：高障碍数量')
    pick(new,'indoor','heading_change_deg',.95,'New / indoor / many turns','新版室内：大累计转向')
    pick(new,'outdoor','route_length_m',.5,'New / outdoor / center route','新版室外：中线行驶',lambda r:not r['side_bias'])
    pick(new,'outdoor','route_length_m',.5,'New / outdoor / right-biased','新版室外：靠右行驶',lambda r:r['side_bias'])
    pick(new,'outdoor','route_length_m',.95,'New / outdoor / long route','新版室外：长路线')
    pick(old,'indoor','repeated_asset_instances',.9,'Legacy / indoor / repeated assets','旧版室内：重复障碍物对照',lambda r:r['repeated_asset_instances']>0)
    pick(old,'outdoor','repeated_asset_instances',.9,'Legacy / outdoor / repeated assets','旧版室外：重复障碍物对照',lambda r:r['repeated_asset_instances']>0)
    pick(old,'outdoor','route_length_m',.5,'Legacy / outdoor / no repeats','旧版室外：无重复对照',lambda r:r['repeated_asset_instances']==0)
    return chosen


def keyframes(z):
    """One frame per temporal third, maximizing front-facing visible obstacle size.

    Approximate visibility uses the saved front-camera mount, 100-degree FOV,
    0.7--8 m distance and original occupancy line-of-sight. This is only a
    review selection heuristic, not a visibility label or a quality metric.
    """
    poses=z['poses_xyyaw']; obstacles=z['obstacle_poses']; dims=z['obstacle_dimensions']
    occupancy=z['occupancy_original']; res=float(z['resolution_m']); origin=z['origin_xy']
    scores=np.zeros(len(poses))
    for i,(x,y,yaw) in enumerate(poses):
        camera=np.array([x+.35*np.cos(yaw),y+.35*np.sin(yaw)])
        delta=obstacles[:,:2]-camera
        distances=np.linalg.norm(delta,axis=1)
        angles=np.arctan2(np.sin(np.arctan2(delta[:,1],delta[:,0])-yaw),np.cos(np.arctan2(delta[:,1],delta[:,0])-yaw))
        for j in np.flatnonzero((distances>.7)&(distances<8)&(np.abs(angles)<np.deg2rad(44))):
            points=camera+np.linspace(0,.97,max(2,int(distances[j]/res)))[:,None]*delta[j]
            cols=np.floor((points[:,0]-origin[0])/res).astype(int)
            rs=occupancy.shape[0]-1-np.floor((points[:,1]-origin[1])/res).astype(int)
            valid=(rs>=0)&(rs<occupancy.shape[0])&(cols>=0)&(cols<occupancy.shape[1])
            if valid.all() and not occupancy[rs,cols].any():
                scores[i]+=float(dims[j,2]/(distances[j]+1)*np.cos(angles[j]))
    parts=np.array_split(np.arange(len(poses)),3)
    def best(part):
        middle=float(np.mean(part))
        return int(max(part,key=lambda i:(scores[i],-abs(i-middle))))
    center=best(parts[1]); gap=max(1,int(np.ceil(len(poses)*.12)))
    selections=[best(parts[0][parts[0]<=center-gap]),center,best(parts[2][parts[2]>=center+gap])]
    return selections, scores[selections].tolist()


def map_figure(z, selected, output):
    occ=z['occupancy_original']; res=float(z['resolution_m']); origin=z['origin_xy']
    route=z['route_control_points']; poses=z['poses_xyyaw']; obstacles=z['obstacle_poses']; dims=z['obstacle_dimensions']
    kinds=z['obstacle_kinds'].tolist(); unique=sorted(set(kinds))
    colors={k:plt.get_cmap('tab20')(i%20) for i,k in enumerate(unique)}
    fig,ax=plt.subplots(figsize=(5.4,8.1),dpi=160)
    fig.patch.set_facecolor('#f8fafc'); ax.set_facecolor('#f8fafc')
    extent=[origin[0],origin[0]+occ.shape[1]*res,origin[1],origin[1]+occ.shape[0]*res]
    ax.imshow(occ,cmap=matplotlib.colors.ListedColormap(['#f0f2ec','#3b4654']),vmin=0,vmax=1,extent=extent,origin='upper',interpolation='nearest')
    ax.plot(route[:,0],route[:,1],'--',color='#06b6d4',lw=1.9,label='Global route')
    ax.plot(poses[:,0],poses[:,1],color='#f97316',lw=1.8,label='Actual rollout')
    for k,((x,y,yaw),dim,kind) in enumerate(zip(obstacles,dims,kinds)):
        corners=np.array([[-1,-1],[-1,1],[1,1],[1,-1]])*dim[:2]/2
        rotation=np.array([[np.cos(yaw),-np.sin(yaw)],[np.sin(yaw),np.cos(yaw)]])
        ax.add_patch(Polygon(corners@rotation.T+[x,y],facecolor=colors[kind],edgecolor='#111827',lw=.6,zorder=5))
        ax.annotate(f'{k+1}',(x,y),xytext=(3,3),textcoords='offset points',fontsize=6,color='#334155',zorder=6)
    ax.scatter(*poses[0,:2],marker='o',c='#22c55e',s=50,edgecolor='white',zorder=7,label='Start')
    ax.scatter(*poses[-1,:2],marker='*',c='#e11d48',s=90,edgecolor='white',zorder=7,label='End')
    for label,idx,color in zip(['A','B','C'],selected,COLORS):
        ax.scatter(*poses[idx,:2],s=58,c=color,edgecolor='white',zorder=8)
        ax.annotate(label,poses[idx,:2],xytext=(6,-10),textcoords='offset points',weight='bold',color=color,zorder=9)
    combined=np.concatenate([route,poses[:,:2],obstacles[:,:2]])
    lower=combined.min(axis=0)-3; upper=combined.max(axis=0)+3
    ax.set_xlim(max(extent[0],lower[0]),min(extent[1],upper[0])); ax.set_ylim(max(extent[2],lower[1]),min(extent[3],upper[1]))
    ax.set_aspect('equal');ax.set_xlabel('World X (m)');ax.set_ylabel('World Y (m)')
    ax.set_title('Saved map + route + rollout',fontsize=11,pad=12)
    ax.legend(loc='upper center',bbox_to_anchor=(.5,-.10),ncol=2,fontsize=8,frameon=False)
    ax.tick_params(labelsize=8); fig.tight_layout()
    fig.savefig(output); plt.close(fig)


def case_figure(row):
    folder=ROOT/row['sample_id']; number=row['case_number']; stem=f'case_{number:02d}'
    with np.load(folder/'rollout.npz',allow_pickle=False) as archive:
        z={k:archive[k] for k in ['poses_xyyaw','obstacle_poses','obstacle_dimensions','obstacle_kinds','occupancy_original','resolution_m','origin_xy','route_control_points','timestamps_s']}
    indices,scores=keyframes(z)
    row['preview_frame_indices']=indices;row['preview_times_s']=[float(z['timestamps_s'][i]) for i in indices]
    row['frame_selection_scores']=scores
    map_path=OUT/(stem+'_map.png');map_figure(z,indices,map_path)
    canvas=Image.new('RGB',(1920,1460),'#f8fafc');draw=ImageDraw.Draw(canvas)
    accent='#0f766e' if row['generation_settings_version']==VERSIONS[1] else '#b45309'
    draw.rectangle((0,0,1920,10),fill=accent)
    draw.text((32,26),f'{number:02d}  {row["title"]}',font=font(32,True),fill='#0f172a')
    draw.text((32,76),row['sample_id'],font=font(21),fill='#64748b')
    draw.text((32,111),f'{row["num_frames"]} frames  |  {row["duration_s"]:.1f} s  |  {row["route_length_m"]:.1f} m  |  {row["obstacle_count"]} obstacles / {row["unique_obstacle_assets"]} unique assets  |  {row["door_count"]} doors',font=font(23),fill='#334155')
    with Image.open(map_path) as im:
        tile=ImageOps.contain(im.convert('RGB'),(525,1010))
        canvas.paste(tile,(16+(525-tile.width)//2,191+(1010-tile.height)//2))
    for column,camera in enumerate(CAMERAS):
        draw.text((553+column*450,166),camera,font=font(23,True),fill='#334155')
    decoded=[]
    for r,(idx,t,color) in enumerate(zip(indices,row['preview_times_s'],COLORS)):
        y=204+r*384
        for c,camera in enumerate(CAMERAS):
            p=folder/'preview'/camera/f'{idx:05d}.jpg'
            with Image.open(p) as im:
                im.load()
                if im.size!=(640,512):raise ValueError(f'Wrong JPEG size: {p}: {im.size}')
                image=im.convert('RGB').resize((438,350),Image.Resampling.LANCZOS)
            x=553+c*450;canvas.paste(image,(x,y));draw.rectangle((x,y,x+437,y+349),outline=color,width=2)
            decoded.append(str(p))
        draw.text((553,y+353),f'{"ABC"[r]}  t={t:.1f}s / frame {idx}',font=font(19,True),fill=color)
    count=collections.Counter(row['obstacle_assets'])
    repeated=[(k,v) for k,v in count.items() if v>1]
    draw.text((32,1226),'Asset multiplicities',font=font(22,True),fill='#0f172a')
    multiplicities=(f'{len(count)} assets x 1 each' if not repeated else ' + '.join(map(str,sorted(count.values(),reverse=True))))+f' = {row["obstacle_count"]}'
    draw.text((32,1262),multiplicities,font=font(21),fill=accent)
    draw.text((32,1298),f'{len(repeated)} asset types repeated',font=font(20),fill='#475569')
    draw.text((32,1369),'A/B/C: one obstacle-focused frame per temporal third. Existing JPEG previews; original RGB remains in visual.npz.',font=font(22),fill='#475569')
    draw.text((32,1405),'Map obstacle colors identify assets within this case. Snapshot: 2026-09-14 21:44:13 UTC+8. New cases 01-06; legacy 07-09.',font=font(21),fill='#64748b')
    path=OUT/(stem+'.png');canvas.save(path)
    row['figure']=path.name;row['map_figure']=map_path.name;row['decoded_previews']=decoded
    # Compact card preserves one synchronized tri-camera set and the map.
    card=Image.new('RGB',(960,640),'#f8fafc');d=ImageDraw.Draw(card)
    d.rectangle((0,0,960,7),fill=accent)
    d.text((18,18),f'{number:02d}  {row["title"]}',font=font(25,True),fill='#0f172a')
    d.text((18,57),f'{row["route_length_m"]:.1f}m | {row["duration_s"]:.1f}s | {row["obstacle_count"]} obs / {row["unique_obstacle_assets"]} unique',font=font(23),fill='#475569')
    card.paste(canvas.crop((16,191,541,1201)).resize((249,480),Image.Resampling.LANCZOS),(6,96))
    best=int(np.argmax(scores));idx=indices[best]
    for c,camera in enumerate(CAMERAS):
        with Image.open(folder/'preview'/camera/f'{idx:05d}.jpg') as im:
            if c==0:tile=im.convert('RGB').resize((475,380),Image.Resampling.LANCZOS);xy=(266,110)
            else:tile=im.convert('RGB').resize((202,162),Image.Resampling.LANCZOS);xy=(748,110+(c-1)*212)
        card.paste(tile,xy)
        d.text((xy[0],xy[1]-22),camera,font=font(17,True),fill='#334155')
    d.text((266,513),f'{"ABC"[best]}  t={row["preview_times_s"][best]:.1f}s | asset counts: {multiplicities}',font=font(20),fill=accent)
    d.text((18,601),row['sample_id'],font=font(21),fill='#475569')
    return card


def statistics_figure(stats):
    fig,axes=plt.subplots(2,2,figsize=(13,8),dpi=160)
    fig.patch.set_facecolor('#f8fafc')
    labels=['Legacy','New v2'];colors=['#d97706','#0f766e']
    x=np.arange(2)
    indoor=[stats[v]['mode_counts']['indoor'] for v in VERSIONS];outdoor=[stats[v]['mode_counts']['outdoor'] for v in VERSIONS]
    ax=axes[0,0];ax.bar(x,indoor,label='Indoor',color='#64748b');ax.bar(x,outdoor,bottom=indoor,label='Outdoor',color='#38bdf8')
    for i in x:
        ax.text(i,indoor[i]/2,str(indoor[i]),ha='center',color='white');ax.text(i,indoor[i]+outdoor[i]/2,str(outdoor[i]),ha='center');ax.text(i,indoor[i]+outdoor[i]+50,str(indoor[i]+outdoor[i]),ha='center',weight='bold')
    ax.set_xticks(x,labels);ax.set_ylim(0,max(np.array(indoor)+outdoor)*1.17);ax.set_title('Trajectory counts');ax.legend()
    ax=axes[0,1];perc=[100*stats[v]['repeated_asset_cases']/stats[v]['metadata_checked_count'] for v in VERSIONS]
    ax.bar(x,perc,color=colors)
    for i in x:ax.text(i,perc[i]+3,f'{perc[i]:.2f}%',ha='center',weight='bold')
    ax.set_xticks(x,labels);ax.set_ylim(0,115);ax.set_title('Trajectories with repeated obstacle assets (%)')
    for ax,key,title in [(axes[1,0],'duration_s','Duration per trajectory (seconds)'),(axes[1,1],'route_length_m','Route length per trajectory (meters)')]:
        groups=[VERSIONS[0]+'/indoor',VERSIONS[1]+'/indoor',VERSIONS[0]+'/outdoor',VERSIONS[1]+'/outdoor']
        values=[stats[g][key] for g in groups]
        ax.bxp([{'label':l,'med':v['median'],'q1':v['p25'],'q3':v['p75'],'whislo':v['min'],'whishi':v['max'],'fliers':[]} for l,v in zip(['Old indoor','New indoor','Old outdoor','New outdoor'],values)],showfliers=False)
        ax.set_title(title);ax.tick_params(axis='x',labelsize=9);ax.grid(axis='y',alpha=.2)
    for ax in axes.flat:ax.spines[['top','right']].set_visible(False)
    fig.suptitle('Simulation dataset | 5,416 trajectories | 2026-09-14 21:44:13 UTC+8',fontsize=16)
    fig.tight_layout(rect=(0,.035,1,.94));fig.text(.5,.015,'Boxes: 25th / 50th / 75th percentiles; whiskers: min / max. Counts from a frozen index; obstacles verified from saved arrays.',ha='center',fontsize=9)
    fig.savefig(OUT/'statistics.png');plt.close(fig)


def main():
    rows=[json.loads(line) for line in (OUT/'audit_records.jsonl').read_text().splitlines()]
    chosen=choose_cases(rows)
    overview=Image.new('RGB',(2880,2018),'#e2e8f0');d=ImageDraw.Draw(overview)
    d.text((24,16),'9 simulation cases  |  6 new + 3 legacy  |  map + synchronized cameras',font=font(34,True),fill='#0f172a')
    for row in chosen:
        card=case_figure(row);i=row['case_number']-1
        overview.paste(card,((i%3)*960,80+(i//3)*646))
        print('Created',row['figure'],row['sample_id'],row['preview_frame_indices'],flush=True)
    overview.save(OUT/'overview_9_cases.jpg',quality=94)
    (OUT/'selected_cases.json').write_text(json.dumps(chosen,ensure_ascii=False,indent=2)+'\n')
    statistics_figure(json.loads((OUT/'statistics.json').read_text()))


if __name__=='__main__':main()
