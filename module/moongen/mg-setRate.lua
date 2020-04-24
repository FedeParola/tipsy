--- Replay a pcap file to measure RFC2544 like throughput

-- TIPSY: Telco pIPeline benchmarking SYstem
--
-- Copyright (C) 2018 by its authors (See AUTHORS)
--
-- This program is free software: you can redistribute it and/or
-- modify it under the terms of the GNU General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- This program is distributed in the hope that it will be useful, but
-- WITHOUT ANY WARRANTY; without even the implied warranty of
-- MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
-- General Public License for more details.
--
-- You should have received a copy of the GNU General Public License
-- along with this program. If not, see <http://www.gnu.org/licenses/>.

local mod = {}

function mod:setRate(dev, rate)
   name_82599 = 'Intel Corporation 82599ES'
   name_X540 = 'Intel Corporation Ethernet Controller 10-Gigabit X540-AT2'
   if dev:getName():sub(1, #name_82599) == name_82599 or
      dev:getName():sub(1, #name_X540) == name_X540 then
      -- "82599 and X540 have per queue rate limit"
      -- https://blog.linuxplumbersconf.org/2012/wp-content/uploads/2012/09/2012-lpc-Hardware-Rate-Limiting-brandeburg.pdf
      for seq, que in pairs(dev.txQueues) do
         -- print('-->  ' .. seq)
         que:setRate(rate)
      end
   else
      dev:setRate(rate)
   end
end

return mod
