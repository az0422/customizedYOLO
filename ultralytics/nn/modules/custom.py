import math

import torch
import torch.nn as nn

from ultralytics.nn.modules import Conv, Conv2, Bottleneck, Detect, DFL, LightConv, GhostConv, RepBottleneck, RepConv, C2f
from ultralytics.utils.tal import TORCH_1_10, dist2bbox, make_anchors

class Groups(nn.Module):
    def __init__(self, groups=2, group_id=0, dim=1):
        super().__init__()
        self.groups = groups
        self.group_id = group_id

    def forward(self, x):
        return x.chunk(self.groups, 1)[self.group_id]

class GroupsF(nn.Module):
    def __init__(self, groups=2, group_id=0):
        super().__init__()
        self.groups = groups
        self.group_id = group_id

    def forward(self, x):
        channels = x.size()[1]
        chunk_index_start = channels // self.groups * self.group_id
        chunk_index_end = channels // self.groups * (self.group_id + 1)

        return x[:, chunk_index_start : chunk_index_end]

class ShuffleConv(nn.Module):
    def __init__(self, c1, c2, k=3, s=1):
        super().__init__()
        assert c1 == c2 or c1 == c2 // 2, "c1 and c2 should be c1 == c2 or c1 == c2 // 2"
        self.flag = c1 == c2
        self.c_in = c1 // 2 if self.flag else c1
        c_out = c1 // 2 if self.flag else c1

        self.conv1 = Conv(c1, c1, 1, 1)
        self.conv2 = Conv(c1, c1, 1, 1)
        self.conv3 = Conv(self.c_in, c_out, k, s)
        self.mp = nn.MaxPool2d(k, s, k // 2)
    
    def forward(self, x):
        if self.flag:
            x1, x2 = self.conv1(x).split((self.c_in, self.c_in), 1)
        else:
            x1, x2 = self.conv1(x), self.conv2(x)

        y1, y2 = self.conv3(x1), self.mp(x2)

        batch, channels, height, width = y1.shape

        result = torch.empty([batch, channels << 1, height, width], device=y1.device)
        result[:, ::2] = y1
        result[:, 1::2] = y2

        return result

class Shortcut(nn.Module):
    def __init__(self):
        super().__init__()
    
    def forward(self, x):
        return x[0] + x[1]
    
class Bagging(nn.Module):
    def __init__(self):
        super().__init__()
    
    def forward(self, x):
        result = x[0]
        
        for xx in x[1:]:
            result = result + xx
        
        return result

class ResidualBlock(nn.Module):
    def __init__(self, c1, c2, ratio=1):
        super().__init__()
        c3 = c2 // ratio
        #if c3 < 8: c3 = 8
        
        conv1 = Conv(c1, c3, 1, 1)
        conv2 = Conv(c3, c2, 3, 1)

        self.m = nn.Sequential(conv1, conv2)

    def forward(self, x):
        return x + self.m(x)

class ResidualBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, ratio=1):
        super().__init__()
        self.m = nn.Sequential(*([ResidualBlock(c1, c2, ratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))

    def forward(self, x):
        return self.m(x)

class ResidualBlock2(nn.Module):
    def __init__(self, c1, c2, ratio=1):
        super().__init__()
        c3 = c2 // ratio
        if c3 < 8: c3 = 8

        conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        conv2 = Conv(c3, c3, 3, 1, None, 1, 1)
        conv3 = Conv(c3, c3, 1, 1, None, 1, 1)
        conv4 = Conv(c3, c2, 3, 1, None, 1, 1)

        self.m = nn.Sequential(conv1, conv2, conv3, conv4)
    
    def forward(self, x):
        return self.m(x) + x

class ResidualBlocks2(nn.Module):
    def __init__(self, c1, c2, n=1, ratio=1):
        super().__init__()
        self.m = nn.Sequential(*([ResidualBlock2(c1, c2, ratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)

class ResidualBlock3(nn.Module):
    def __init__(self, c1, c2, ratio=1):
        super().__init__()
        c3 = c2 // ratio
        self.m = nn.Sequential(Conv(c1, c3, 1, 1), Conv(c3, c3, 3, 1), Conv(c3, c2, 1, 1))
    
    def forward(self, x):
        return self.m(x) + x

class ResidualBlocks3(nn.Module):
    def __init__(self, c1, c2, n=1, ratio=1):
        super().__init__()
        self.m = nn.Sequential(*([ResidualBlock3(c1, c2, ratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)

class CSPResidualBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, e=1):
        super().__init__()
        self.conv1 = Conv(c1, c2 // 2, 1, 1)
        self.conv2 = Conv(c1, c2 // 2, 1, 1)
        self.conv3 = Conv(c2, c2, 1, 1)
        
        self.m = nn.Sequential(*[ResidualBlock(c2 // 2, c2 // 2, e) for _ in range(n)])
    
    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x)
        
        y = self.m(x2)
        
        return self.conv3(torch.cat([x1, y], axis=1))

class FuseResidualBlock(nn.Module):
    def __init__(self, c1, c2, e=1.0):
        super().__init__()
        c3 = int(c1 * e)
        if c3 < 8: c3 = 8
        
        self.conv1 = Conv(c1, c3, 1, 1)
        self.conv2 = Conv(c3, c2, 3, 1)

    def forward(self, x):
        return x + self.conv2(self.conv1(x))
    
    def forward_fuse(self, x):
        return self.conv2(self.conv1(x))

class FuseResidualBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, e=1.0):
        super().__init__()
        self.m = nn.Sequential(*([FuseResidualBlock(c1, c2, e) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))

    def forward(self, x):
        return self.m(x)

class SEBlock(nn.Module):
    def __init__(self, c1, ratio=16):
        super(SEBlock, self).__init__()
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.fc1 = nn.Linear(c1, c1 // ratio)
        self.relu = nn.ReLU(inplace=True)
        self.fc2 = nn.Linear(c1 // ratio, c1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        batch_size, channels, _, _ = x.size()

        # Squeeze
        y = self.pool(x).view(batch_size, channels)

        # Excitation
        y = self.sigmoid(self.fc2(self.relu(self.fc1(y))))

        # Scale
        y = y.view(batch_size, channels, 1, 1)
        return x * y

class SEResidualBlock(nn.Module):
    def __init__(self, c1, c2, ratio=16):
        super().__init__()
        self.se = SEBlock(c1, ratio)
        self.conv = Conv(c1, c2, 3, 1, None, 1, 1)
    
    def forward(self, x):
        return x + self.conv(self.se(x))

class SEResidualBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, ratio=16):
        super().__init__()
        self.m = nn.Sequential(*([SEResidualBlock(c1, c2, ratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)
    
    
class PoolResidualBlock(nn.Module):
    def __init__(self, c1, c2, pool_kernel=5):
        super().__init__()

        self.pool = nn.MaxPool2d(pool_kernel, 1, pool_kernel // 2)
        self.conv = Conv(c1, c2, 3, 1, None, 1, 1)

    def forward(self, x):
        return x + self.conv(self.pool(x))

class PoolResidualBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, pool_kernel=5):
        super().__init__()
        self.m = nn.Sequential(*([PoolResidualBlock(c1, c2, pool_kernel) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))

    def forward(self, x):
        return self.m(x)

class DWResidualBlock(nn.Module):
    def __init__(self, c1, c2, dwratio=1):
        super().__init__()
        self.conv1 = Conv(c1, c2, 3, 1, None, c1 // dwratio, 1)
        self.conv2 = Conv(c2, c2, 1, 1, None, 1, 1)
    
    def forward(self, x):
        return x + self.conv2(self.conv1(x))

class DWResidualBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1):
        super().__init__()
        
        self.m = nn.Sequential(*([DWResidualBlock(c1, c2, dwratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)

class CSPDWResidualBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1):
        super().__init__()
        c3 = c2 // 2
        
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv3 = Conv(c2, c2, 1, 1, None, 1, 1)
        self.m = DWResidualBlocks(c3, c3, n, dwratio)
    
    def forward(self, x):
        a = self.conv1(x)
        b = self.conv2(x)
        y = self.m(b)

        return self.conv3(torch.cat([a, y], axis=1))

class DWResidualBlock2(nn.Module):
    def __init__(self, c1, c2, dwratio=1, btratio=1):
        super().__init__()

        c3 = c2 // btratio
        
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c3, c3, 3, 1, None, c3 // dwratio, 1)
        self.conv3 = Conv(c3, c3, 1, 1, None, 1, 1)
        self.conv4 = Conv(c3, c2, 3, 1, None, c2 // dwratio if btratio == 1 else 1, 1)
    
    def forward(self, x):
        return x + self.conv4(self.conv3(self.conv2(self.conv1(x))))

class DWResidualBlocks2(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__()
        self.m = nn.Sequential(*([DWResidualBlock2(c1, c2, dwratio, btratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)

class CSPDWResidualBlocks2(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__()
        c3 = c2 // 2
        
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv3 = Conv(c2, c2, 1, 1, None, 1, 1)
        self.m = DWResidualBlocks2(c3, c3, n, dwratio, btratio)
    
    def forward(self, x):
        a = self.conv1(x)
        b = self.conv2(x)
        y = self.m(b)

        return self.conv3(torch.cat([a, y], axis=1))

class DWResidualBlock3(nn.Module):
    def __init__(self, c1, c2, dwratio=1, btratio=1):
        super().__init__()
        c3 = c2 // btratio

        conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        conv2 = Conv(c3, c3, 3, 1, None, c3 // dwratio if dwratio != 0 else 1, 1)
        conv3 = Conv(c3, c2, 1, 1, None, 1, 1)

        self.m = nn.Sequential(conv1, conv2, conv3)
    
    def forward(self, x):
        return x + self.m(x)

class DWResidualBlocks3(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__()

        self.m = nn.Sequential(*([DWResidualBlock3(c1, c2, dwratio, btratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)

class CSPDWResidualBlocks3(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__()
        c3 = c2 // 2
        
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv3 = Conv(c2, c2, 1, 1, None, 1, 1)
        self.m = DWResidualBlocks3(c3, c3, n, dwratio, btratio)
    
    def forward(self, x):
        a = self.conv1(x)
        b = self.conv2(x)
        y = self.m(b)

        return self.conv3(torch.cat([a, y], axis=1))

class C2Tiny(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__()
        
        self.conv1 = Conv(c1, c2, 1, 1, None, 1, 1)
        self.conv2 = Conv(c2, c2, 1, 1, None, 1, 1)
        self.m = nn.Sequential(*[DWResidualBlock3(c2 // 2, c2 // 2, dwratio, btratio) for _ in range(n)])
    
    def forward(self, x):
        a, b = self.conv1(x).chunk(2, 1)
        x1 = self.m(a)
        return self.conv2(torch.cat([b, x1], axis=1))

class C2TinyF(C2f):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__(c1, c2)

        self.c = c2 // 2
        self.conv1 = Conv(c1, c2, 1, 1, None, 1, 1)
        self.conv2 = Conv((n + 2) * (c2 // 2), c2, 1, 1, None, 1, 1)
        self.m = nn.ModuleList([DWResidualBlock3(c2 // 2, c2 // 2, dwratio, btratio) for _ in range(n)])
    
    def forward(self, x):
        x1 = list(self.conv1(x).chunk(2, 1))
        x1.extend(m(x1[-1]) for m in self.m)
        return self.conv2(torch.cat(x1, 1))
    
    def forward_split(self, x):
        x1 = list(self.conv1(x).split((self.c, self.c), 1))
        x1.extend(m(x1[-1]) for m in self.m)
        return self.conv2(torch.cat(x1, 1))


class C2Aug(nn.Module):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__()

        self.conv1 = Conv(c1, c2, 1, 1, None, 1, 1)
        self.conv2 = Conv(c2, c2, 1, 1, None, 1, 1)

        self.conv3 = Conv(c2 // 2, c2 // 4, 1, 1, None, 1, 1)
        self.conv4 = Conv(c2 // 4, c2 // 4, 3, 1, None, c2 // 4, 1)

        self.m = nn.Sequential(*[DWResidualBlock3(c2 // 2, c2 // 2, dwratio, btratio) for _ in range(n)])
    
    def forward(self, x):
        a, b = self.conv1(x).chunk(2, 1)
        y1 = self.m(a)
        x2 = self.conv3(b)
        y2 = self.conv4(x2)

        return self.conv2(torch.cat([y1, y2, x2], axis=1))

class C2AugF(C2f):
    def __init__(self, c1, c2, n=1, dwratio=1, btratio=1):
        super().__init__(c1, c2)

        self.c = c2 // 2

        self.conv1 = Conv(c1, c2, 1, 1, None, 1, 1)
        self.conv2 = Conv((3 + n) * (c2 // 2), c2, 1, 1, None, 1, 1)

        self.conv3 = Conv(c2 // 2, c2 // 4, 1, 1, None, 1, 1)
        self.conv4 = Conv(c2 // 4, c2 // 4, 3, 1, None, c2 // 4, 1)

        self.m = nn.ModuleList([DWResidualBlock3(c2 // 2, c2 // 2, dwratio, btratio) for _ in range(n)])
    
    def forward(self, x):
        x1 = list(self.conv1(x).chunk(2, 1))
        x1.extend(m(x1[-1]) for m in self.m)
        x2 = x1[0]
        y1 = self.conv3(x2)
        y2 = self.conv4(y1)
        x1.append(torch.cat([y1, y2], 1))

        return self.conv2(torch.cat(x1, 1))
    
    def forward_split(self, x):
        x1 = list(self.conv1(x).split((self.c, self.c), 1))
        x1.extend(m(x1[-1]) for m in self.m)
        x2 = x1[0]
        y1 = self.conv3(x2)
        y2 = self.conv4(y1)
        x1.append(torch.cat([y1, y2], 1))

        return self.conv2(torch.cat(x1, 1))


class ResNextBlock(nn.Module):
    def __init__(self, c1, c2, expand=1.0, dwratio=1):
        super().__init__()
        
        c3 = int(c1 * expand)
        
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c3, c3, 3, 1, None, c3 // dwratio, 1)
        self.conv3 = Conv(c3, c2, 1, 1, None, 1, 1)
        
    def forward(self, x):
        return x + self.conv3(self.conv2(self.conv1(x)))

class ResNextBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, expand=1.0, dwratio=1):
        super().__init__()
        self.m = nn.Sequential(*([ResNextBlock(c1, c2, expand, dwratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)
        
class EfficientBlock(nn.Module):
    def __init__(self, c1, c2, expand=6, ratio=16, stride=1):
        super().__init__()
        c3 = int(c1 * expand)
        self.stride = stride
        
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c3, c3, 3, stride, None, c3, 1)
        self.conv3 = Conv(c3, c2, 1, 1, None, 1, 1, None)
        self.se = SEBlock(c3, ratio)

    def forward(self, x):
        y = self.conv3(self.se(self.conv2(self.conv1(x))))
        
        if self.stride == 1:
            return x + y
        return y

class EfficientBlocks(nn.Module):
    def __init__(self, c1, c2, n=1, expand=6, ratio=16):
        super().__init__()
        
        self.m = nn.Sequential(*([EfficientBlock(c1, c2, expand, ratio) for _ in range(n)] + [Conv(c2, c2, 1, 1)]))
    
    def forward(self, x):
        return self.m(x)

class CSPEfficientBlock(nn.Module):
    def __init__(self, c1, c2, expand=6, ratio=16):
        super().__init__()
        
        self.conv1 = Conv(c1, c2 // 2, 1, 1)
        self.conv2 = Conv(c1, c2 // 2, 1, 1)
        self.efficient = EfficientBlock(c2 // 2, c2 // 2, expand, ratio, 1)
        
        self.conv3 = Conv(c2, c2, 1, 1)
    
    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x)
        y1 = self.efficient(x2)
        
        return self.conv3(torch.cat([x1, y1], axis=1))

class InceptionBlock(nn.Module):
    def __init__(self, c1, c2):
        c3 = c2 // 4
        
        super().__init__()
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c3, c3, 3, 1, None, 1, 1)
        self.conv3 = Conv(c3, c3, 3, 1, None, 1, 1)

        self.conv4 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv5 = Conv(c3, c3, 3, 1, None, 1, 1)

        self.pool = nn.MaxPool2d(5, 1, 2)
        self.conv6 = Conv(c1, c3, 1, 1, None, 1, 1)

        self.conv7 = Conv(c1, c3, 1, 1, None, 1, 1)

    def forward(self, x):
        y1 = self.conv3(self.conv2(self.conv1(x)))
        y2 = self.conv5(self.conv4(x))
        y3 = self.conv6(self.pool(x))
        y4 = self.conv7(x)
        return torch.cat([y1, y2, y3, y4], axis=1)

class CSPInceptionBlock(nn.Module):
    def __init__(self, c1, c2):
        super().__init__()
        
        self.conv1 = Conv(c1, c2 // 2, 1, 1)
        self.conv2 = Conv(c1, c2 // 2, 1, 1)
        self.inception = InceptionBlock(c2 // 2, c2 // 2)
        
        self.conv3 = Conv(c2, c2, 1, 1)
    
    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x)
        y1 = self.inception(x2)
        return self.conv3(torch.cat([x1, y1], axis=1))

class XceptionBlock(nn.Module):
    def __init__(self, c1, c2, ratio=4):
        super().__init__()
        
        self.conv1 = Conv(c1, c2, 1, 1, None, 1, 1)
        self.conv2 = Conv(c2, c2, 3, 1, None, c2 // ratio, 1)
        
    def forward(self, x):
        return self.conv2(self.conv1(x))

class CSPXceptionBlock(nn.Module):
    def __init__(self, c1, c2, ratio=4):
        super().__init__()
        
        self.conv1 = Conv(c1, c2 // 2, 1, 1)
        self.conv2 = Conv(c1, c2 // 2, 1, 1)
        self.xception = XceptionBlock(c2 // 2, c2 // 2, ratio)
        
        self.conv3 = Conv(c2, c2, 1, 1)
    
    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x)
        y1 = self.xception(x2)
        return self.conv3(torch.cat([x1, y1], axis=1))

class MobileBlock(nn.Module):
    def __init__(self, c1, c2, stride=1):
        super().__init__()
        self.stride = stride
        
        self.conv1 = Conv(c1, c1, 3, stride, None, c1, 1)
        self.conv2 = Conv(c1, c2, 1, 1, None, 1, 1)
    
    def forward(self, x):
        return self.conv2(self.conv1(x))

class MobileBlockv2(nn.Module):
    def __init__(self, c1, c2, stride=1, t=6):
        super().__init__()
        self.stride = stride
        c3 = c1 * t
        
        self.conv1 = Conv(c1, c3, 1, 1, None, 1, 1)
        self.conv2 = Conv(c3, c3, 3, stride, None, c3, 1)
        self.conv3 = Conv(c3, c2, 1, 1, None, 1, 1, None)
    
    def forward(self, x):
        y = self.conv3(self.conv2(self.conv1(x)))
        
        if self.stride == 1 and y.shape[1] == x.shape[1]:
            return x + y
        
        return y

class CSPMobileBlock(nn.Module):
    def __init__(self, c1, c2):
        super().__init__()
        
        self.conv1 = Conv(c1, c2 // 2, 1, 1)
        self.conv2 = Conv(c1, c2 // 2, 1, 1)
        self.mobile = MobileBlock(c2 // 2, c2 // 2)
        
        self.conv3 = Conv(c2, c2, 1, 1)
    
    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x)
        y1 = self.mobile(x2)
        return self.conv3(torch.cat([x1, y1], axis=1))

class FireModule(nn.Module):
    def __init__(self, c1, c2, expand=1):
        super().__init__()
        c3 = c2 // expand

        self.conv1 = Conv(c1, c3, 1, 1)
        self.conv2 = Conv(c3 // 2, c2 // 2, 1, 1)
        self.conv3 = Conv(c3 // 2, c2 // 2, 3, 1)
    
    def forward(self, x):
        x1, x2 = self.conv1(x).chunk(2, 1)
        return x + torch.cat([self.conv2(x1), self.conv3(x2)], 1)

class FireC2(nn.Module):
    def __init__(self, c1, c2, n=1, expand=1):
        super().__init__()
        self.conv1 = Conv(c1, c2, 1, 1)
        self.conv2 = Conv(c2, c2, 1, 1)

        self.m = nn.Sequential(*[FireModule(c2 // 2, c2 // 2, expand) for _ in range(n)])
    
    def forward(self, x):
        x1, x2 = self.conv1(x).chunk(2, 1)
        return self.conv2(torch.cat([x1, self.m(x2)], 1))

class FireC3(nn.Module):
    def __init__(self, c1, c2, n=1, expand=1):
        super().__init__()
        self.conv1 = Conv(c1, c2 // 2, 1, 1)
        self.conv2 = Conv(c1, c2 // 2, 1, 1)
        self.conv3 = Conv(c2, c2, 1, 1)
        
        self.m = nn.Sequential(*[FireModule(c2 // 2, c2 // 2, expand) for _ in range(n)])
    
    def forward(self, x):
        return self.conv3(torch.cat([self.conv1(x), self.m(self.conv2(x))], 1))

class SPPCSP(nn.Module):
    def __init__(self, c1, c2, k=(5, 9, 13)):
        super().__init__()
        c3 = c2 // 2

        self.cv1 = Conv(c1, c3, 1, 1)
        self.cv2 = Conv(c1, c3, 1, 1)
        self.cv3 = Conv(c3 * 4, c3, 1, 1)
        self.cv4 = Conv(c3 * 2, c2, 1, 1)

        self.m1 = nn.MaxPool2d(kernel_size=k[0], stride=1, padding=k[0] // 2)
        self.m2 = nn.MaxPool2d(kernel_size=k[1], stride=1, padding=k[1] // 2)
        self.m3 = nn.MaxPool2d(kernel_size=k[2], stride=1, padding=k[2] // 2)

    def forward(self, x):
        x1 = self.cv1(x)
        x2 = self.cv2(x)
        spp1 = self.m1(x1)
        spp2 = self.m2(x1)
        spp3 = self.m3(x1)
        y1 = self.cv3(torch.cat([x1, spp1, spp2, spp3], 1))
        return self.cv4(torch.cat([x2, y1], 1))

class SPPFCSP(nn.Module):
    def __init__(self, c1, c2, k=5):
        super().__init__()
        c3 = c2 // 2

        self.conv1 = Conv(c1, c3, 1, 1)
        self.conv2 = Conv(c1, c3, 1, 1)
        self.conv3 = Conv(c3 * 4, c3, 1, 1)
        self.conv4 = Conv(c3 * 2, c2, 1, 1)

        self.m = nn.MaxPool2d(kernel_size=k, stride=1, padding=k // 2)

    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x)
        y1 = self.m(x2)
        y2 = self.m(y1)
        y3 = self.m(y2)
        return self.conv4(torch.cat([x1, self.conv3(torch.cat([x2, y1, y2, y3], 1))], 1))

class SPPFCSPF(nn.Module):
    def __init__(self, c1, c2, k=5):
        super().__init__()
        c3 = c2 // 2

        self.conv1 = Conv(c1, c3, 1, 1)
        self.conv2 = Conv(c1, c3, 1, 1)
        self.conv3 = Conv(c3 * 5, c2, 1, 1)

        self.m = nn.MaxPool2d(kernel_size=k, stride=1, padding=k // 2)

    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x)
        y1 = self.m(x2)
        y2 = self.m(y1)
        y3 = self.m(y2)
        return self.conv3(torch.cat([x1, x2, y1, y2, y3], 1))

class AuxiliaryShortcut(nn.Module):
    def __init__(self, ratio=0.3):
        super().__init__()
        self.ratio = ratio
    
    def forward(self, x):
        return x[0] + x[1] * self.ratio
    
    def forward_fuse(self, x):
        return x[0]

class RepC2f(C2f):
    def __init__(self, c1, c2, n=1):
        super().__init__(c1, c2)
        self.c = c2 // 2
        self.cv1 = Conv(c1, c2, 1, 1)
        self.cv2 = Conv((2 + n) * c2 // 2, c2, 1, 1)
        self.m = nn.ModuleList(RepConv(c2 // 2, c2 // 2) for _ in range(n))

    def forward(self, x):
        """Forward pass through C2f layer."""
        y = list(self.cv1(x).chunk(2, 1))
        y.extend(m(y[-1]) for m in self.m)
        return self.cv2(torch.cat(y, 1))

    def forward_split(self, x):
        """Forward pass using split() instead of chunk()."""
        y = list(self.cv1(x).split((self.c, self.c), 1))
        y.extend(m(y[-1]) for m in self.m)
        return self.cv2(torch.cat(y, 1))

class RELAN(nn.Module):
    def __init__(self, c1, c2, n=2):
        super().__init__()
        self.c = c2 // 2
        self.cv1 = Conv(c1, c2, 1, 1)
        self.cv2 = Conv((2 + n) * self.c, c2, 1, 1)
        self.m1 = nn.ModuleList(nn.Sequential(Conv(self.c, self.c // 2, 1, 1), RepConv(self.c // 2, self.c // 2, 3, 1), Conv(self.c // 2, self.c, 1, 1)) for _ in range(n))
    
    def forward(self, x):
        x1, x2 = self.cv1(x).split((self.c, self.c), 1)
        y1 = [x1, x2]
        y1.extend((m(y1[-1]) + y1[-1]) for m in self.m1)

        return self.cv2(torch.cat(y1, 1))


# ------------------------------------------------------------------------------

class HeaderConvTiny(nn.Module):
    def __init__(self, c1, c2):
        super().__init__()
        
        conv1 = Conv(c1, c2, 1, 1)
        conv2 = LightConv(c2, c2, 3, 1)

        self.m = nn.Sequential(conv1, conv2)
    
    def forward(self, x):
        return self.m(x)

class DetectorTiny(Detect):
    def __init__(self, nc, ch=()):
        super().__init__(nc, ch)

        c2, c3 = self.reg_max * 4, self.nc

        self.cv2 = nn.ModuleList(nn.Sequential(HeaderConvTiny(x, c2), nn.Conv2d(c2, self.reg_max * 4, 1)) for x in ch)
        self.cv3 = nn.ModuleList(nn.Sequential(HeaderConvTiny(x, c3), nn.Conv2d(c3, self.nc, 1)) for x in ch)

class HeaderConvTinyv2(nn.Module):
    def __init__(self, c1, c2):
        super().__init__()

        conv1 = Conv(c1, c2, 1, 1)
        conv2 = LightConv(c2, c2, 3, 1)

        self.m = nn.Sequential(conv1, conv2)
    
    def forward(self, x):
        return self.m(x)

class DetectorTinyv2(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        c2 = self.reg_max * 4  # channels
        c3 = self.nc

        self.cv1 = nn.ModuleList(HeaderConvTinyv2(x, self.no) for x in ch)
        self.cv2 = nn.ModuleList(nn.Conv2d(c2, c2, 1) for _ in ch)
        self.cv3 = nn.ModuleList(nn.Conv2d(c3, c3, 1) for _ in ch)

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            t = self.cv1[i](x[i])
            x[i] = torch.cat((self.cv2[i](t[:, :self.reg_max * 4]), self.cv3[i](t[:, self.reg_max * 4:])), 1)
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self  # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
            a.bias.data[:] = 1.0  # box
            b.bias.data[:] = math.log(5 / m.nc / (640 / s) ** 2)  # cls (.01 objects, 80 classes, 640 img)
    
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class DetectorTinyv3(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        c2 = self.reg_max * 4  # channels
        c3 = self.nc

        self.cv1 = nn.ModuleList(HeaderConvTinyv2(x, self.no) for x in ch)
        self.cv2 = nn.ModuleList(nn.Conv2d(self.no, c2, 1) for _ in ch)
        self.cv3 = nn.ModuleList(nn.Conv2d(self.no, c3, 1) for _ in ch)

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            t = self.cv1[i](x[i])
            x[i] = torch.cat([self.cv2[i](t), self.cv3[i](t)], axis=1)
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self  # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
            a.bias.data[:] = 1.0  # box
            b.bias.data[:] = math.log(5 / m.nc / (640 / s) ** 2)  # cls (.01 objects, 80 classes, 640 img)
    
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class DetectorTinyv4(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        c1 = self.reg_max * 4 + 32
        c2 = self.reg_max * 4  # channels
        c3 = self.nc

        self.cv1 = nn.ModuleList(HeaderConvTinyv2(x, c1) for x in ch)
        self.cv2 = nn.ModuleList(nn.Conv2d(c1, c2, 1) for _ in ch)
        self.cv3 = nn.ModuleList(nn.Conv2d(c1, c3, 1) for _ in ch)

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            t = self.cv1[i](x[i])
            x[i] = torch.cat([self.cv2[i](t), self.cv3[i](t)], axis=1)
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self  # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
            a.bias.data[:] = 1.0  # box
            b.bias.data[:] = math.log(5 / m.nc / (640 / s) ** 2)  # cls (.01 objects, 80 classes, 640 img)

    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class DetectorPrototype(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        c1 = self.reg_max * 4 + self.nc

        self.cv2 = nn.ModuleList(nn.Sequential(Conv(x, c1, 1, 1), nn.Conv2d(c1, c1, 1, groups=c1)) if x != c1 else nn.Sequential(nn.Conv2d(c1, c1, 1, groups=c1)) for x in ch)

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            x[i] = self.cv2[i](x[i])
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self  # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        for a, s in zip(m.cv2, m.stride):  # from
            a[-1].bias.data[:m.reg_max * 4] = 1.0  # box
            a[-1].bias.data[m.reg_max * 4:] = math.log(5 / m.nc / (640 / s) ** 2)
    
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class DetectorTinyv5(DetectorTinyv4):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__(nc, ch)
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        c1 = self.reg_max * 4 + 32
        c2 = self.reg_max * 4  # channels
        c3 = self.nc

        self.cv1 = nn.ModuleList(nn.Sequential(Conv(x, c1, 1 if x > c1 else 3, 1), LightConv(c1, c1, 3, 1)) for x in ch)
        self.cv2 = nn.ModuleList(nn.Conv2d(c1, c2, 1) for _ in ch)
        self.cv3 = nn.ModuleList(nn.Conv2d(c1, c3, 1) for _ in ch)

class DetectorTinyv6(DetectorTinyv4):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__(nc, ch)
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        c1 = self.reg_max * 4 * 2
        c2 = self.reg_max * 4  # channels
        c3 = self.nc

        self.cv1 = nn.ModuleList(nn.Sequential(Conv(x, c1, 1 if x > c1 else 3, 1), LightConv(c1, c1, 3, 1)) for x in ch)
        self.cv2 = nn.ModuleList(nn.Conv2d(c1, c2, 1) for _ in ch)
        self.cv3 = nn.ModuleList(nn.Conv2d(c1, c3, 1) for _ in ch)

class DetectorPrototype2(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        c1 = self.reg_max * 4 * 2
        c2 = self.reg_max * 4  # channels
        c3 = self.nc

        self.cv2 = nn.ModuleList(nn.Conv2d(c2, c2, 1) for _ in ch)
        self.cv3 = nn.ModuleList(nn.Conv2d(c3, c3, 1) for _ in ch)

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            x[i] = torch.cat([self.cv2[i](x[i][:, :self.reg_max * 4]), self.cv3[i](x[i][:, self.reg_max * 4:])], axis=1)
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self  # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        
        for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
            a.bias.data[:] = 1.0  # box
            b.bias.data[:] = math.log(5 / m.nc / (640 / s) ** 2)  # cls (.01 objects, 80 classes, 640 img)
    
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class DetectorPrototype3(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, k=3, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        self.filter = []

        self.cv1 = nn.ModuleList(nn.Conv2d(x, self.reg_max * 4 + nc, k, 1, padding=k // 2) for x in ch)

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            x[i] = self.cv1[i](x[i])
            x[i][:, :self.reg_max * 4] += 1.0
            x[i][:, self.reg_max * 4:] += self.filter[i] if len(self.filter) != 0 else 0
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self
        # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        # for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
        #     a.bias.data[:] = 1.0  # box
        for s in m.stride:
            m.filter.append(math.log(5 / m.nc / (640 / s) ** 2))  # cls (.01 objects, 80 classes, 640 img)
        
    
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)
    
class DetectorPrototype4(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80, k=3, ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        self.filter = []

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            x[i][:, :self.reg_max * 4] += 1.0
            x[i][:, self.reg_max * 4:] += self.filter[i] if len(self.filter) != 0 else 0
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self
        # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        # for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
        #     a.bias.data[:] = 1.0  # box
        for s in m.stride:
            m.filter.append(math.log(5 / m.nc / (640 / s) ** 2))  # cls (.01 objects, 80 classes, 640 img)
        
    
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class NDetectAux(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80,ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        self.filter = []
        c1 = self.reg_max * 2
        c2 = self.reg_max * 4 + self.nc

        self.cv1 = nn.ModuleList(nn.Sequential(Conv(x, x, 3, 1), Conv(x, c1, 1, 1), Conv(c1, c2, 3, 1), nn.Conv2d(c2, c2, 1, 1, bias=False)) for x in ch[::2])
        self.cv2 = nn.ModuleList(nn.Sequential(Conv(x, x, 3, 1), Conv(x, c1, 1, 1), Conv(c1, c2, 3, 1), nn.Conv2d(c2, c2, 1, 1, bias=False)) for x in ch[1::2])

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(0, self.nl, 2):
            x[i] = self.cv1[i//2](x[i])
            x[i][:, :self.reg_max * 4] += 1.0
            x[i][:, self.reg_max * 4:] += self.filter[i] if len(self.filter) != 0 else 0

            x[i + 1] = self.cv2[i//2](x[i + 1])
            x[i + 1][:, :self.reg_max * 4] += 1.0
            x[i + 1][:, self.reg_max * 4:] += self.filter[i + 1] if len(self.filter) != 0 else 0
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)
    
    def forward_fuse(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(0, self.nl, 2):
            x[i] = self.cv1[i//2](x[i])
            x[i][:, :self.reg_max * 4] += 1.0
            x[i][:, self.reg_max * 4:] += self.filter[i] if len(self.filter) != 0 else 0
        
        x = x[::2]

        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride[::2], 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)
    
    def del_attr(self):
        self.__delattr__("cv2")

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self
        # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        # for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
        #     a.bias.data[:] = 1.0  # box
        for s in m.stride:
            m.filter.append(math.log(5 / m.nc / (640 / s) ** 2))  # cls (.01 objects, 80 classes, 640 img)
        
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class NDetectAuxDual(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80,ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        self.filter = []
        c1 = self.reg_max * 2
        c2 = self.reg_max * 4 + self.nc

        self.cv1 = nn.ModuleList(nn.Sequential(Conv(x, x, 3, 1), Conv(x, c1, 1, 1), Conv(c1, c2, 3, 1), nn.Conv2d(c2, c2, 1, 1, bias=True)) for x in ch[::3])
        self.cv2 = nn.ModuleList(nn.Sequential(Conv(x, x, 3, 1), Conv(x, c1, 1, 1), Conv(c1, c2, 3, 1), nn.Conv2d(c2, c2, 1, 1, bias=True)) for x in ch[1::3])
        self.cv3 = nn.ModuleList(nn.Sequential(Conv(x, x, 3, 1), Conv(x, c1, 1, 1), Conv(c1, c2, 3, 1), nn.Conv2d(c2, c2, 1, 1, bias=True)) for x in ch[2::3])

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(0, self.nl, 3):
            x[i] = self.cv1[i//3](x[i])
            x[i][:, :self.reg_max * 4] += 1.0
            x[i][:, self.reg_max * 4:] += self.filter[i] if len(self.filter) != 0 else 0

            x[i + 1] = self.cv2[i//3](x[i + 1])
            x[i + 1][:, :self.reg_max * 4] += 1.0
            x[i + 1][:, self.reg_max * 4:] += self.filter[i + 1] if len(self.filter) != 0 else 0

            x[i + 2] = self.cv3[i//3](x[i + 2])
            x[i + 2][:, :self.reg_max * 4] += 1.0
            x[i + 2][:, self.reg_max * 4:] += self.filter[i + 2] if len(self.filter) != 0 else 0
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)
    
    def forward_fuse(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(0, self.nl, 3):
            x[i] = self.cv1[i//3](x[i])
            x[i][:, :self.reg_max * 4] += 1.0
            x[i][:, self.reg_max * 4:] += self.filter[i] if len(self.filter) != 0 else 0
        
        x = x[::3]

        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride[::3], 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)
    
    def del_attr(self):
        self.__delattr__("cv2")
        self.__delattr__("cv3")

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self
        # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        # for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
        #     a.bias.data[:] = 1.0  # box
        for s in m.stride:
            m.filter.append(math.log(5 / m.nc / (640 / s) ** 2))  # cls (.01 objects, 80 classes, 640 img)
        
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)

class NDetect(nn.Module):
    """YOLOv8 Detect head for detection models."""
    dynamic = False  # force grid reconstruction
    export = False  # export mode
    shape = None
    anchors = torch.empty(0)  # init
    strides = torch.empty(0)  # init

    def __init__(self, nc=80,ch=()):
        """Initializes the YOLOv8 detection layer with specified number of classes and channels."""
        super().__init__()
        self.nc = nc  # number of classes
        self.nl = len(ch)  # number of detection layers
        self.reg_max = 16  # DFL channels (ch[0] // 16 to scale 4/8/12/16/20 for n/s/m/l/x)
        self.no = nc + self.reg_max * 4  # number of outputs per anchor
        self.stride = torch.zeros(self.nl)  # strides computed during build

        self.filter = []
        c1 = self.reg_max * 2
        c2 = self.reg_max * 4 + self.nc

        self.cv1 = nn.ModuleList(nn.Sequential(Conv(x, x, 3, 1), Conv(x, c1, 1, 1), Conv(c1, c2, 3, 1), nn.Conv2d(c2, c2, 1, 1)) for x in ch)

        self.dfl = DFL(self.reg_max) if self.reg_max > 1 else nn.Identity()

    def forward(self, x):
        """Concatenates and returns predicted bounding boxes and class probabilities."""
        shape = x[0].shape  # BCHW
        for i in range(self.nl):
            x[i] = self.cv1[i](x[i])
            x[i][:, :self.reg_max * 4] += 1.0
            x[i][:, self.reg_max * 4:] += self.filter[i] if len(self.filter) != 0 else 0
        if self.training:
            return x
        elif self.dynamic or self.shape != shape:
            self.anchors, self.strides = (x.transpose(0, 1) for x in make_anchors(x, self.stride, 0.5))
            self.shape = shape

        x_cat = torch.cat([xi.view(shape[0], self.no, -1) for xi in x], 2)
        if self.export and self.format in ('saved_model', 'pb', 'tflite', 'edgetpu', 'tfjs'):  # avoid TF FlexSplitV ops
            box = x_cat[:, :self.reg_max * 4]
            cls = x_cat[:, self.reg_max * 4:]
        else:
            box, cls = x_cat.split((self.reg_max * 4, self.nc), 1)
        dbox = dist2bbox(self.dfl(box), self.anchors.unsqueeze(0), xywh=True, dim=1) * self.strides

        if self.export and self.format in ('tflite', 'edgetpu'):
            # Normalize xywh with image size to mitigate quantization error of TFLite integer models as done in YOLOv5:
            # https://github.com/ultralytics/yolov5/blob/0c8de3fca4a702f8ff5c435e67f378d1fce70243/models/tf.py#L307-L309
            # See this PR for details: https://github.com/ultralytics/ultralytics/pull/1695
            img_h = shape[2] * self.stride[0]
            img_w = shape[3] * self.stride[0]
            img_size = torch.tensor([img_w, img_h, img_w, img_h], device=dbox.device).reshape(1, 4, 1)
            dbox /= img_size

        y = torch.cat((dbox, cls.sigmoid()), 1)
        return y if self.export else (y, x)

    def bias_init(self):
        """Initialize Detect() biases, WARNING: requires stride availability."""
        m = self
        # self.model[-1]  # Detect() module
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1
        # ncf = math.log(0.6 / (m.nc - 0.999999)) if cf is None else torch.log(cf / cf.sum())  # nominal class frequency
        # for a, b, s in zip(m.cv2, m.cv3, m.stride):  # from
        #     a.bias.data[:] = 1.0  # box
        for s in m.stride:
            m.filter.append(math.log(5 / m.nc / (640 / s) ** 2))  # cls (.01 objects, 80 classes, 640 img)
        
    def decode_bboxes(self, bboxes, anchors):
        """Decode bounding boxes."""
        return dist2bbox(bboxes, anchors, xywh=True, dim=1)